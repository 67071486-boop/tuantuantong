"""`/api/auth/site`、`/api/auth/me` 的两键、以及「我的资料」自助端点（spec §5）。

⚠️ 本文件的一切夹具值都是**合成值**（PII 守卫扫 `src/**`）——⛔ 不要写真实工作室名 / 真实昵称。
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_context, tenant_context

from apps.accounts.models import StaffProfile
from apps.core.error_codes import ErrorCode
from apps.platform.bootstrap import PLATFORM_DOMAIN, ensure_public_tenant
from apps.platform.models import RealNameGrant, Tenant
from apps.realname.models import RealNameCertRecord, RealNameSetting
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import P2_TEST_PASSWORD, P2_TEST_USERNAME, make_logged_in_client

#: 合成的工作室名（本文件所有 `tenant_name` 断言都用它）。
FAKE_STUDIO = "示例工作室甲"

#: `TenantTestCase` 的租户域名（与 `make_logged_in_client` 的默认值同源）。
#: ⚠️ **匿名请求必须显式打在这个 host 上**：`TenantTestCase` 的 `self.client` 用的是 Django
#: 默认 `SERVER_NAME="testserver"`，而 `TenantMainMiddleware` 在域名表里找不到它 ⇒ 在进
#: urlconf **之前**就抛 `Http404`（HTML 响应）。那与「路由不存在」的 404 状态码相同、形状不同，
#: 会让本文件第一条用例**即使路由已实现也永远红**（实测：现有 `/api/auth/csrf` 经
#: `self.client` 是 404、经本 host 是 200）。同型约定见 `tests/test_operation_log_events_api.py`。
TENANT_DOMAIN = "tenant.test.com"


class _TenantNameMixin:
    """把测试租户的名字设成合成值。

    ⚠️ 必须**显式设**：`TenantTestCase` 造租户时不传 `name`（`Tenant.name` 落成 `""`），
    若拿 `self.tenant.name` 当期望值，断言就变成 `"" == ""` 的**恒真判据**（证明不了任何事）。
    用 `.update()` 而不是 `tenant.save()`：后者在 `TenantMixin.save()` 里有
    「只能在 public 或自己 schema 里更新」的守卫，测试期连接在自己 schema 里虽能过，
    但没必要依赖那条边。
    """

    def setUp(self):
        super().setUp()
        Tenant.objects.filter(pk=self.tenant.pk).update(name=FAKE_STUDIO)


class SiteEndpointTests(_TenantNameMixin, TenantTestCase):
    """`GET /api/auth/site` —— 公开回当前租户（工作室）名（spec §5.5）。"""

    def test_anonymous_can_read_tenant_name(self):
        # 匿名就能拿：登录页在登录前要显示它（对标 `/Login/GetCompany` 无需凭据）。
        res = Client(SERVER_NAME=TENANT_DOMAIN).get("/api/auth/site")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["tenant_name"], FAKE_STUDIO)

    def test_request_params_do_not_change_the_answer(self):
        # 传参不能改租户上下文：这是 AGENTS.md §4 的硬规则在端点上的判据。
        res = Client(SERVER_NAME=TENANT_DOMAIN).get(
            "/api/auth/site", {"tenant_name": "捏造的工作室", "schema": "other"}
        )
        self.assertEqual(res.json()["data"]["tenant_name"], FAKE_STUDIO)


class UserPayloadTests(_TenantNameMixin, TenantTestCase):
    """`/api/auth/me` 与登录响应新增 `tenant_name` / `role`（spec §5.4）。"""

    def test_me_carries_tenant_name_and_single_role(self):
        client = make_logged_in_client(groups=("客服",))
        user = client.get("/api/auth/me").json()["data"]["user"]
        self.assertEqual(user["tenant_name"], FAKE_STUDIO)
        # 单值职位：持多个 Group 时取**名称升序首个**（与 `account_payload` 同一口径）。
        self.assertEqual(user["role"], "客服")
        # `roles` 保留（首页「我的职位」那一格仍拼全部角色）。
        self.assertEqual(user["roles"], ["客服"])

    def test_role_is_empty_string_without_any_group(self):
        client = make_logged_in_client()
        user = client.get("/api/auth/me").json()["data"]["user"]
        self.assertEqual(user["role"], "")

    def test_role_with_multiple_groups_is_the_name_ascending_first(self):
        """多 Group 时取**名称升序首个**（不是建组 / 入库顺序）：口径走 `_first_role_name`。

        ⚠️ 建组顺序刻意**反着**来（先「派单员」后「客服」）：`sorted()` 的答案是「客服」
        （客 U+5BA2 < 派 U+6D3E）。若有人把复用改成 `groups.first()`（顺序由数据库返回顺序
        决定），这条会红——单 Group 的用例抓不到这个漂移。
        """
        client = make_logged_in_client(groups=("派单员", "客服"))
        user = client.get("/api/auth/me").json()["data"]["user"]
        self.assertEqual(user["role"], "客服")
        # `roles` 是 `values_list` 的结果，顺序无保证 ⇒ 比集合、不比列表。
        self.assertEqual(sorted(user["roles"]), ["客服", "派单员"])

    def test_login_response_carries_the_two_keys(self):
        """登录响应与 `/me` **共形**：`_user_payload` 的两个调用点都要把 `request` 传进去。

        ⚠️ 单测 `/me` 抓不到登录那一处漏传 `request`（漏了只表现为 `tenant_name` 恒空串）——
        而前端登录后是**直接吃登录响应**填侧栏的，所以这条必须单独钉。
        """
        get_user_model().objects.create_user(username="probe-role", password="pw-probe-role")
        res = Client(SERVER_NAME=TENANT_DOMAIN).post(
            "/api/auth/login",
            data={"username": "probe-role", "password": "pw-probe-role"},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        user = res.json()["data"]["user"]
        self.assertEqual(user["tenant_name"], FAKE_STUDIO)
        self.assertEqual(user["role"], "")


class MyProfileReadTests(TenantTestCase):
    def test_anonymous_is_rejected(self):
        # ⚠️ 必须显式给租户域名（`self.client` 的 SERVER_NAME 是 `testserver`，
        #    中间件找不到 Domain 行 ⇒ HTML 404，断言会在别处红）。见本文件顶注释。
        self.assertEqual(Client(SERVER_NAME=TENANT_DOMAIN).get("/api/auth/profile").status_code, 401)

    def test_payload_shape_and_derived_real_name_flag(self):
        client = make_logged_in_client(groups=("客服",))
        profile = StaffProfile.objects.get(user__username=P2_TEST_USERNAME)
        profile.id_card_no = "110101199001011234"
        profile.alipay_account = "acc@example.com"
        profile.base_salary = "0.00"
        profile.save()
        data = client.get("/api/auth/profile").json()["data"]["profile"]
        self.assertEqual(data["username"], P2_TEST_USERNAME)
        self.assertEqual(data["role"], "客服")
        self.assertTrue(data["is_real_name"])               # 派生字段，不是新列
        self.assertEqual(data["id_card_no"], "110101199001011234")
        self.assertEqual(
            sorted(data),
            sorted({"username", "display_name", "role", "id_card_no", "is_real_name",
                    "base_salary", "alipay_account", "alipay_name", "bank_name",
                    "bank_card_no", "qq", "mobile", "live_url", "games",
                    # ⭐ 图片存储线：两个**只读**的短时效媒体链接。
                    #    ⚠️ 它们是派生物、不是可写列 ⇒ ⛔ 没进 `EDITABLE_FIELDS`（那会开出自助写面）。
                    "avatar_url", "wechat_qr_url",
                    # ⭐ 实名线（T7）：`{enabled, remaining}` 两个只读事实。
                    #    ⚠️ 同上：它是**只读子结构**、⛔ 不在 `EDITABLE_FIELDS` 里（开关由管理员页改）。
                    "realname"}),
        )

    def test_missing_profile_row_does_not_raise(self):
        # `games=None` = 不建档案（夹具语义见 p5_auth_helpers docstring）。
        client = make_logged_in_client(groups=("客服",), games=None)
        res = client.get("/api/auth/profile")
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]["profile"]
        self.assertEqual(data["id_card_no"], "")
        self.assertIsNone(data["base_salary"])
        self.assertEqual(data["games"], [])

    def test_tenant_name_empty_string_is_200_not_404(self):
        """Task 1 评审 Minor #2 的钉子（controller 要求并入本任务）。

        判据：租户没有名字时 `/api/auth/site` 回 **200 + `""`**，⛔ 不是 404（spec §5.5）。
        ⚠️ 这一支**可达**：`TenantTestCase` 造租户时本来就不传 `name`（所以 Task 1 的
        `_TenantNameMixin` 才要显式设名）——把名字清空就是「取不到」的真实形态。
        """
        Tenant.objects.filter(pk=self.tenant.pk).update(name="")
        res = Client(SERVER_NAME=TENANT_DOMAIN).get("/api/auth/site")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["tenant_name"], "")


class MyProfileWriteTests(TenantTestCase):
    EDITABLE = {
        "alipay_account": "a@example.com",
        "alipay_name": "测试姓名甲",
        "bank_name": "测试银行",
        "bank_card_no": "6222000000000000",
        "qq": "10000000",
        "mobile": "13800000000",
        "live_url": "https://example.invalid/room",
    }

    #: ⚠️ **本类所有「要走到视图体」的用例都必须显式挂 `my_profile.edit`**（夹具 `nodes`
    #: 默认空集 ⇒ 不挂就是 fail-closed 的 403，判据根本到不了 `patch()` 的方法体）。
    #: 这是对 task-2-brief 里那几段测试代码的**必要修正**：brief 直接写
    #: `make_logged_in_client(groups=("客服",))` 却没给节点，而它自己的
    #: `test_without_node_is_forbidden_but_admin_short_circuits` 又要求同一形状必须 403
    #: ⇒ 两处自相矛盾；不带节点的那几条会以 403 红、而不是它断言的形状。
    NODE = ("my_profile.edit",)

    def test_all_seven_fields_round_trip(self):
        from apps.audit.models import OperationLog

        client = make_logged_in_client(groups=("客服",), nodes=self.NODE)
        res = client.patch("/api/auth/profile", self.EDITABLE, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["code"], ErrorCode.OK)
        self.assertEqual(res.json()["data"]["profile"]["mobile"], "13800000000")
        # ⚠️ 多字段一次提交 ⇒ 日志**恰好一行**（不是逐字段 7 行）。
        # 单字段用例（`test_audit_row_…`）抓不到「有人把 `record()` 挪进 for 循环」：
        # 那种坏法下每条用例都只改一个字段、行数仍是 1 ⇒ 全绿。这里用 7 个字段钉住它。
        self.assertEqual(OperationLog.objects.filter(action="user.self_update").count(), 1)
        # 回读一致（不靠响应自证：再 GET 一次）
        again = client.get("/api/auth/profile").json()["data"]["profile"]
        for key, value in self.EDITABLE.items():
            self.assertEqual(again[key], value, key)

    def test_server_managed_keys_are_rejected(self):
        """⚠️ 校验失败在本仓是 **HTTP 200 + code=ErrorCode.VALIDATION**（不是 400）。

        口径出处：`apps/core/exceptions.py` 的 `envelope_exception_handler`——
        「业务结果（资源不存在、校验失败）：HTTP 200，code 取 ErrorCode」。
        判据因此必须打在 `code` 与 `msg` 上，⛔ 不能打 HTTP 状态码。
        """
        client = make_logged_in_client(groups=("客服",), nodes=self.NODE)
        # ⚠️ 四个服务端管理的键**全都要覆盖**（spec §12.1 点名的就是这四个）：
        # 只采样 `username` / `games` 时，谁把 `id_card_no` 或 `base_salary` 从
        # `_validate_only_editable` 的拒绝面里漏掉都不会红。
        res = client.patch(
            "/api/auth/profile",
            {
                "username": "hacker",
                "id_card_no": "110101199001011234",
                "games": [1],
                "base_salary": "1.00",
            },
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION)
        # 四个越界键都要在**同一句** msg 里（错误信封只带第一条消息，所以实现要把它们并成一句）
        self.assertIn("username", body["msg"])
        self.assertIn("games", body["msg"])
        self.assertIn("id_card_no", body["msg"])
        self.assertIn("base_salary", body["msg"])

    def test_non_object_body_is_validation_not_500(self):
        """非对象请求体（JSON 数组）⇒ 200 + `code=VALIDATION`，⛔ 不是 500。

        E-0010 同族：DRF 的 `JSONParser` 不校验顶层类型，数组会原样成为 `request.data`；
        不先挡就 `.keys()` 抛 `AttributeError` ⇒ 500。⚠️ 本路由**不在**
        `tests/test_no_route_500.py` 的枚举来源里（只收 business / audit 两个模块）⇒
        这条判据没有结构网兜底，必须在这里显式钉住。
        """
        client = make_logged_in_client(groups=("客服",), nodes=self.NODE)
        res = client.patch("/api/auth/profile", data="[]", content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["code"], ErrorCode.VALIDATION)

    def test_creating_row_on_demand(self):
        client = make_logged_in_client(groups=("客服",), games=None, nodes=self.NODE)
        res = client.patch(
            "/api/auth/profile", {"mobile": "13800000000"}, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            StaffProfile.objects.get(user__username=P2_TEST_USERNAME).mobile, "13800000000"
        )

    def test_without_node_is_forbidden_but_admin_short_circuits(self):
        from apps.core.profile_views import MyProfileView

        # 普通角色**不给** `my_profile.edit` ⇒ 403，msg 是本视图自己的那句（可指认）
        client = make_logged_in_client(groups=("客服",))
        res = client.patch("/api/auth/profile", {"qq": "1"}, content_type="application/json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["msg"], MyProfileView.NODE_DENIED_MESSAGE)
        # ⚠️ 同时钉**字面量**（E-0024）：只比常量时把 `NODE_DENIED_MESSAGE` 改成英文默认句
        # 也不会红——而「中文界面直接显示它」正是这个常量存在的唯一理由。
        self.assertEqual(res.json()["msg"], "当前职位没有此操作所需的权限节点")
        # 租户管理员（持「系统管理员」）短路放行 ⇒ 200
        admin = make_logged_in_client(username="myprofile-admin", groups=("系统管理员",))
        self.assertEqual(
            admin.patch(
                "/api/auth/profile", {"qq": "1"}, content_type="application/json"
            ).status_code,
            200,
        )

    def test_audit_row_is_written_once_and_only_on_change(self):
        from apps.audit.models import OperationLog

        client = make_logged_in_client(groups=("客服",), nodes=self.NODE)
        before = OperationLog.objects.filter(action="user.self_update").count()
        client.patch("/api/auth/profile", {"qq": "10000000"}, content_type="application/json")
        after = OperationLog.objects.filter(action="user.self_update").count()
        self.assertEqual(after - before, 1)
        # 同值重提交 = 无变化 ⇒ **不再写一行**（幂等保存不留痕）
        client.patch("/api/auth/profile", {"qq": "10000000"}, content_type="application/json")
        self.assertEqual(OperationLog.objects.filter(action="user.self_update").count(), after)
        row = OperationLog.objects.filter(action="user.self_update").latest("id")
        self.assertIn("修改渠道", row.summary)
        # 渠道、不是字段值（收款账号 / 卡号这类 PII 不进日志）
        self.assertNotIn("10000000", row.summary)
        # ⚠️ **「字段值一个都不进日志」必须钉到三个列上**：
        # 模板只渲染 `{reason}` ⇒ 只看 `summary` 的旧判据在「有人把值塞进
        # `from_value` / `to_value` / `field_name`」时**照样绿**。
        self.assertEqual(row.reason, "系统网页")
        self.assertEqual(row.field_name, "")
        self.assertEqual(row.from_value, "")
        self.assertEqual(row.to_value, "")
        # ⚠️ 钉**字面量**（E-0024）：原先写 `row.actor_id == OperationLog.objects.latest("id").actor_id`
        # 是**恒真**的自指断言（`row` 自己就是最新一行）⇒ 改成直接核操作人用户名。
        self.assertEqual(row.actor, P2_TEST_USERNAME)


class SerializerWidthTests(TestCase):
    def test_serializer_widths_match_model(self):
        from apps.accounts.models import StaffProfile
        from apps.core.profile_views import EDITABLE_FIELDS, MyProfileWriteSerializer

        for name in EDITABLE_FIELDS:
            self.assertEqual(
                MyProfileWriteSerializer().fields[name].max_length,
                StaffProfile._meta.get_field(name).max_length,
                f"{name} 的列宽在序列化器与模型之间漂移了",
            )


#: 跨租户隔离用例的第二个租户（域名与 schema 名都与租户 A 不同）。
DOMAIN_A = TENANT_DOMAIN
DOMAIN_B = "myprofile-iso-b.test.local"
B_SCHEMA = "myprofile_iso_b"

#: 两个租户各自的标记用户名：**租户内唯一、且两侧取值不同**。
#: ⛔ 不用主键指认「另一租户的那一行」——两个 schema 的 pk 序列各自从 1 开始（E-0011），
#: 用 pk 断言会恒真/恒假（`AGENTS.md` §5G 的实测坑）。
A_USER = "profile-iso-a"
B_USER = "profile-iso-b"


class MyProfileCrossTenantTests(TenantTestCase):
    """AGENTS.md §4 / spec §12.1：以租户 A 的凭据**不能**读写 B 的资料。

    ⚠️ 骨架照 `tests/test_account_isolation.py`：
    - 第二个租户在 `setUpClass` 建（schema DDL 不能在 `TestCase` 的事务里回滚）；
    - B 侧一切操作都走 `_b_call()`——跨域名请求**不会**在响应结束时还原 schema
      （`TenantMainMiddleware` 只在请求进来时 `set_tenant`），不钉回来会读到错的库。

    ⚠️ 判别力：两侧各写**不同**的 `mobile` / `qq`，断言 A 只看到 A 的那一份、
    且 A 的写入没有污染 B。⛔ 不用 pk（两 schema 序列都从 1 起，见上）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=B_SCHEMA, name="我的资料隔离租户B", domain=DOMAIN_B
        )

    def setUp(self):
        # 第一行：挡住上一条用例残留的 schema（见模块说明同型坑）。
        connection.set_tenant(self.tenant)

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def _b_call(self, function):
        """在 B 的 schema 上下文里执行 `function()`，**结束后把连接钉回租户 A**。"""
        try:
            with tenant_context(self.tenant_b):
                return function()
        finally:
            connection.set_tenant(self.tenant)

    def test_tenant_a_never_reads_or_writes_tenant_b_profile(self):
        a_values = {"mobile": "13900000001", "qq": "10000001"}
        b_values = {"mobile": "13900000002", "qq": "10000002"}
        node = ("my_profile.edit",)

        client_a = make_logged_in_client(
            domain=DOMAIN_A, username=A_USER, groups=("客服",), nodes=node
        )
        # 夹具发过跨域名请求 ⇒ 显式钉回 A（见 `_b_call` 的理由）。
        connection.set_tenant(self.tenant)
        client_b = self._b_call(
            lambda: make_logged_in_client(
                domain=DOMAIN_B, username=B_USER, groups=("客服",), nodes=node
            )
        )

        self.assertEqual(
            client_a.patch(
                "/api/auth/profile", a_values, content_type="application/json"
            ).status_code,
            200,
        )
        self.assertEqual(
            self._b_call(
                lambda: client_b.patch(
                    "/api/auth/profile", b_values, content_type="application/json"
                )
            ).status_code,
            200,
        )

        # A 的凭据只看到 A 自己的行：用户名与值是 A 的那一份。
        profile_a = client_a.get("/api/auth/profile").json()["data"]["profile"]
        self.assertEqual(profile_a["username"], A_USER)
        self.assertEqual(profile_a["mobile"], a_values["mobile"])
        self.assertEqual(profile_a["qq"], a_values["qq"])
        self.assertNotEqual(profile_a["username"], B_USER, "A 的凭据看到了 B 的账号")

        # 数据库行判据（E-0009）：A 的 schema 里是 A 写的值，且看不到 B 的账号
        # （用户名两侧取值不同 ⇒ 这条否定断言有判别力，不是恒真）。
        user_model = get_user_model()
        row_a = StaffProfile.objects.get(user__username=A_USER)
        self.assertEqual(row_a.mobile, a_values["mobile"])
        self.assertEqual(row_a.qq, a_values["qq"])
        self.assertFalse(user_model.objects.filter(username=B_USER).exists())

        # 反向：A 的写入没有污染 B —— B 仍是 B 自己写的值，且 B 的 schema 里看不到 A。
        profile_b = self._b_call(
            lambda: client_b.get("/api/auth/profile").json()["data"]["profile"]
        )
        self.assertEqual(profile_b["username"], B_USER)
        self.assertEqual(profile_b["mobile"], b_values["mobile"])
        self.assertEqual(profile_b["qq"], b_values["qq"])
        row_b = self._b_call(lambda: StaffProfile.objects.get(user__username=B_USER))
        self.assertEqual(row_b.mobile, b_values["mobile"])
        self.assertEqual(row_b.qq, b_values["qq"])
        self.assertFalse(self._b_call(lambda: user_model.objects.filter(username=A_USER).exists()))

    def test_site_returns_each_tenants_own_name(self):
        """`GET /api/auth/site` 的租户名必须由 **Host** 决定（spec §5.5）。

        ⚠️ 原先只在租户 A 上验过它 ⇒ 「实现改成 `Tenant.objects.first()` 那种形状」
        （或任何不读 Host 的全局取法）不会红。这里给 A / B 设**不同**的合成名、各打自己的
        域名，两边必须是各自的：取同一个名字时其中一条必红。
        """
        Tenant.objects.filter(pk=self.tenant.pk).update(name="隔离租户甲")
        Tenant.objects.filter(pk=self.tenant_b.pk).update(name="隔离租户乙")
        name_a = Client(SERVER_NAME=DOMAIN_A).get("/api/auth/site").json()["data"]["tenant_name"]
        connection.set_tenant(self.tenant)  # 跨域名请求不还原 schema，钉回来（同 `_b_call` 的理由）
        name_b = self._b_call(
            lambda: Client(SERVER_NAME=DOMAIN_B).get("/api/auth/site").json()["data"]["tenant_name"]
        )
        self.assertEqual(name_a, "隔离租户甲")
        self.assertEqual(name_b, "隔离租户乙")
        self.assertNotEqual(name_a, name_b, "两个域名拿到了同一个租户名 ⇒ 没在读 Host")


class MyProfileOnThePublicPlaneTests(TenantTestCase):
    """平台平面（`public`）没有 `StaffProfile` / 角色目录 ⇒ 明确 404（spec §5.6）。

    ⚠️ 口径（controller 收紧）：**已认证 ⇒ 404；匿名 ⇒ 401**——认证排在业务判据之前
    是本仓固定次序（`IsAuthenticated` 先于视图体里的 `_plane_guard`）。

    ⚠️ 骨架照 `tests/test_account_admin_predicate.py`：`ensure_public_tenant` 放 `setUp`
    （测试事务内）而**不是** `setUpClass`——后者会被提交并活过整个进程；
    `tearDown` 把连接钉回租户平面（跨域名请求不还原 schema）。
    """

    def setUp(self):
        super().setUp()
        ensure_public_tenant(PLATFORM_DOMAIN)
        with schema_context("public"):
            get_user_model().objects.create_superuser(
                username="myprofile-pub-super", email="", password="pw-myprofile-pub"
            )
        self.client = Client(SERVER_NAME=PLATFORM_DOMAIN)
        login = self.client.post(
            "/api/auth/login",
            data={"username": "myprofile-pub-super", "password": "pw-myprofile-pub"},
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200, login.content[:200])

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def test_authenticated_request_is_404_not_500(self):
        """已认证：GET/PATCH 都回 404（⛔ 不是 500，也不是 403）。

        ⚠️ 404 而不是 403 是**判据设计**：`public` 平面跳过节点门（否则 `has_node` 恒假、
        请求会先被 403 挡掉，「平面没有此功能」这条就永远测不到，controller resolution #5）。
        """
        res = self.client.get("/api/auth/profile")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["code"], 404)
        # ⚠️ 钉**字面量**（E-0024）：profile 与 password 两条路现在共用
        # `apps/core/plane.py::plane_not_found_response`；把 msg 改坏（或两处又各写一份）
        # 时这条与下面 password 的那条必须一起红。
        self.assertEqual(res.json()["msg"], "当前平面没有此功能")
        res = self.client.patch(
            "/api/auth/profile", {"qq": "1"}, content_type="application/json"
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["code"], 404)
        self.assertEqual(res.json()["msg"], "当前平面没有此功能")

    def test_anonymous_request_is_401(self):
        """匿名：401（认证先于平面判据——不许把匿名请求伪装成「平面没有此功能」）。"""
        anon = Client(SERVER_NAME=PLATFORM_DOMAIN)
        self.assertEqual(anon.get("/api/auth/profile").status_code, 401)
        self.assertEqual(
            anon.patch(
                "/api/auth/profile", {"qq": "1"}, content_type="application/json"
            ).status_code,
            401,
        )

    def test_password_endpoint_is_404_when_authenticated(self):
        # `self.client` 在这个类里已是 public 平面已登录态（见该类的 setUp）。
        res = self.client.post(
            "/api/auth/password",
            {"old_password": "x", "new_password": "yyyyyy", "confirm_password": "yyyyyy"},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["code"], 404)
        # 与 profile 那两条共用 `apps/core/plane.py::plane_not_found_response` 的同一个 msg。
        self.assertEqual(res.json()["msg"], "当前平面没有此功能")

    def test_password_endpoint_is_401_when_anonymous(self):
        res = Client(SERVER_NAME=PLATFORM_DOMAIN).post(
            "/api/auth/password", {}, content_type="application/json"
        )
        self.assertEqual(res.status_code, 401)


class ChangePasswordTests(TenantTestCase):
    """`POST /api/auth/password` —— 自助改密（spec §5.3）。

    ⚠️ 校验失败在本仓是 **HTTP 200 + code=ErrorCode.VALIDATION**（不是 400）——
    判据打在 `code` 与 `msg` 上；只有「未登录」才是 HTTP 401。
    """

    def _post(self, client, old="", new="", confirm=""):
        return client.post(
            "/api/auth/password",
            {"old_password": old, "new_password": new, "confirm_password": confirm},
            content_type="application/json",
        )

    def _client(self):
        """带 `home.change_password` 节点的登录客户端。

        ⚠️ **必须显式给节点**（Task 2 的实测教训）：`make_logged_in_client` 的 `nodes` **默认空集**
        ⇒ 不给的话每个用例都会在**视图体之前**被节点门 403 挡掉，「校验失败」那些判据永远测不到。

        ⚠️ **还必须显式给一个 Group**（本任务执行期实测：只写 `nodes=…` 时这几条全红在 403）：
        节点是挂在 `Group → Role` 上的，而 `_grant_nodes` 遍历的是 `user.groups`
        （`tests/p5_auth_helpers.py`）——**没有组就没有可挂的 Role，`nodes=…` 会被静默忽略**
        ⇒ 节点门照样 403、而失败信息只说「没权限」，看起来像判据写错。
        Task 2 的写测试用 `groups=("客服",)` 正是这个原因；这里照抄，不另造形状。
        """
        return make_logged_in_client(groups=("客服",), nodes=("home.change_password",))

    def test_anonymous_is_rejected(self):
        # ⚠️ 租户域名要显式给（`self.client` 打不到租户路由，见文件顶注释）。
        res = self._post(Client(SERVER_NAME=TENANT_DOMAIN), P2_TEST_PASSWORD, "newpw123", "newpw123")
        self.assertEqual(res.status_code, 401)

    def test_wrong_old_password(self):
        client = self._client()
        res = self._post(client, "definitely-wrong", "newpw123", "newpw123")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["code"], ErrorCode.VALIDATION)
        self.assertIn("原密码", res.json()["msg"])

    def test_wrong_old_password_error_comes_before_new_password_rules(self):
        """⚠️ 安全属性：老口令错时**不得**泄露新口令的规则（判据要能分辨校验顺序）。

        `test_wrong_old_password` 用的是**合法**新口令 ⇒ 把实现改成「先查新口令格式」在它
        下面**照样绿**。这里让 `new` 同时**不合法**：顺序若被调换，msg 会变成
        「新密码长度…」——不知道老口令的人由此学到新口令的规则。
        """
        client = self._client()
        res = self._post(client, "definitely-wrong", "12345", "12345")
        self.assertEqual(res.json()["code"], ErrorCode.VALIDATION)
        self.assertIn("原密码", res.json()["msg"])

    def test_length_bounds_are_inclusive(self):
        client = self._client()
        for bad in ("12345", "1" * 19):
            res = self._post(client, P2_TEST_PASSWORD, bad, bad)
            self.assertEqual(res.json()["code"], ErrorCode.VALIDATION, bad)
            # ⚠️ 钉**区间字面量**而不是裸 `"6"`：`assertIn("6", …)` 会被 "18"/"16" 满足，
            # 把上界写坏的实现也放过去（E-0024 族：断言要钉值）。
            self.assertIn("6–18", res.json()["msg"])
        # 下界 6 放行（前面的失败没有改到口令，所以这里老口令仍然有效）
        ok = self._post(client, P2_TEST_PASSWORD, "123456", "123456")
        self.assertEqual(ok.json()["code"], ErrorCode.OK)
        # 上界 18 的**放行**正例（此前只有越界 19 被拒，放行侧没有钉子）：
        # 此刻口令已是 "123456"，再改成 18 位。
        ok18 = self._post(client, "123456", "1" * 18, "1" * 18)
        self.assertEqual(ok18.json()["code"], ErrorCode.OK)

    def test_confirm_mismatch(self):
        client = self._client()
        res = self._post(client, P2_TEST_PASSWORD, "newpw123", "newpw124")
        self.assertEqual(res.json()["code"], ErrorCode.VALIDATION)
        self.assertIn("不一致", res.json()["msg"])

    def test_same_as_old_is_rejected(self):
        client = self._client()
        res = self._post(client, P2_TEST_PASSWORD, P2_TEST_PASSWORD, P2_TEST_PASSWORD)
        self.assertEqual(res.json()["code"], ErrorCode.VALIDATION)
        self.assertIn("原密码相同", res.json()["msg"])

    def test_without_node_is_forbidden(self):
        """没有 `home.change_password` 的账号 ⇒ 403（节点门真的在拦）。"""
        res = self._post(make_logged_in_client(), P2_TEST_PASSWORD, "newpw123", "newpw123")
        self.assertEqual(res.status_code, 403)

    def test_non_object_body_is_validation_not_500(self):
        """非对象请求体（JSON 数组）⇒ 200 + `code=VALIDATION`，⛔ 不是 500。

        E-0010 同族（与 `MyProfileWriteTests` 的同名用例一条判据）：DRF 的 `JSONParser`
        不校验顶层类型，数组会原样成为 `request.data`；不先挡就 `.get()` 抛 `AttributeError`
        ⇒ 500。⚠️ 本路由**不在** `tests/test_no_route_500.py` 的枚举来源里（只收
        business / audit 两个模块）⇒ 这条判据没有结构网兜底，必须在这里显式钉住。
        """
        client = self._client()
        res = client.post(
            "/api/auth/password", data="[]", content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["code"], ErrorCode.VALIDATION)

    def test_non_string_password_value_is_rejected_and_never_changes_the_password(self):
        """⚠️ E-0010 的**值**这一半：容器是 `dict` 不代表里头的值是字符串。

        真值非字符串**一律 HTTP 500**（**读码实测**，不是「静默改坏口令」）：

        1. `len(12345678)` / `len(True)` 抛 `TypeError` ⇒ 不是 `APIException` ⇒
           `apps/core/exceptions.py` 的信封处理器返回 None ⇒ **HTTP 500**；
        2. 就算长度那关先被绕过，`set_password(list)` 也会 500：
           `django/contrib/auth/hashers.py::make_password` 在**哈希之前**就对非
           `str` / `bytes` 抛 `TypeError: Password must be a string or bytes, got list.`
           ⇒ **不存在**「`force_bytes(list)` 把 `str(list)` 当口令哈希 ⇒ 回 200 而账号被
           静默锁死」那条路径。⚠️ 本 docstring 原先（并标「实测」）断言的正是那条路径——
           2026-09-24 复核为**错的**（错根因比没有根因更贵），按 §5C 改成上面这条结论。

        ⇒ **承重**判据是 `200 + code=VALIDATION + msg 含「字符串」**；末尾「原口令仍能登录」
        是**双保险**（防的是「类型关以后被挪到 `check_password` 之后」这类回归），不是主判据。
        """
        client = self._client()
        # ⚠️ 前四个是**真值**形状：`["a"] * 8` 长度 8 落在 6–18、new 与 confirm 又是同一个值
        # （长度关 / 相等关都拦不住它）；`12345678` / `True` 覆盖「`len()` 直接抛 `TypeError`」。
        # ⚠️ 后四个是**假值**形状：`[]` / `{}` / `0` / `False` 会被
        # `raw or ""` 归一成 `""` ⇒ 走「长度 0」那条分支、msg 变成「新密码长度需为 6–18 位」
        # ⇒ 生产代码若把 `"" if raw is None else raw` 改回 `raw or ""`，这四条就红。
        for bad in (["a"] * 8, ["aaaaaa"], {"a": 1}, 12345678, True, [], {}, 0, False):
            res = self._post(client, P2_TEST_PASSWORD, bad, bad)
            self.assertEqual(res.status_code, 200, bad)
            self.assertEqual(res.json()["code"], ErrorCode.VALIDATION, bad)
            self.assertIn("字符串", res.json()["msg"], bad)
        # ⚠️ **双保险、不是承重判据**：上面的 200 + `code=VALIDATION` + 「字符串」才是承重的那条。
        # 这一跳防的是「类型关以后被挪到 `check_password` 之后」这类回归（那时 `set_password`
        # 会先 500，或真把口令改掉）——它证明「拒绝之后口令没被改坏」，但**不能**证明
        # 「拒绝的原因是类型关」（例如类型关被挪走但口令恰好没变时它仍然绿）。
        fresh = Client(SERVER_NAME=self.domain.domain)
        self.assertEqual(
            fresh.post(
                "/api/auth/login",
                {"username": P2_TEST_USERNAME, "password": P2_TEST_PASSWORD},
                content_type="application/json",
            ).status_code,
            200,
            "原口令已经登不进去 ⇒ 口令真被改掉了（⛔ 机制不是「非字符串值静默改坏」："
            "Django 的 make_password 对非 str/bytes 抛 TypeError ⇒ HTTP 500；"
            "这条红了说明类型关没拦住、请求走到了会改口令的成功分支）",
        )

    def test_success_keeps_session_and_swaps_password(self):
        from apps.audit.models import OperationLog

        client = self._client()
        logs_before = OperationLog.objects.count()
        res = self._post(client, P2_TEST_PASSWORD, "newpw123", "newpw123")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["code"], ErrorCode.OK)
        # 保持登录（`update_session_auth_hash`）：同一 client 仍能读 /api/auth/me。
        self.assertEqual(client.get("/api/auth/me").status_code, 200)
        # 新口令可登录、旧口令不行（判据打在**真实登录出口**上，不是看数据库列）
        fresh = Client(SERVER_NAME=self.domain.domain)
        self.assertEqual(
            fresh.post("/api/auth/login",
                       {"username": P2_TEST_USERNAME, "password": P2_TEST_PASSWORD},
                       content_type="application/json").status_code,
            401,
            "旧口令仍然能登录 ⇒ 改密没有真的生效",
        )
        self.assertEqual(
            fresh.post("/api/auth/login",
                       {"username": P2_TEST_USERNAME, "password": "newpw123"},
                       content_type="application/json").status_code,
            200,
        )
        # 刻意不记操作日志（裁定 #4 同族）——判据打在数据库行数上（E-0009）。
        self.assertEqual(OperationLog.objects.count(), logs_before)


# ══════════════════════════════════════════════════════════════════════════════
# T7（实名线）：`/api/auth/profile` 载荷里的 `realname` 子结构
# ══════════════════════════════════════════════════════════════════════════════
#: 合成的 18 位号（**不指向任何真人**）。⚠️ 本文件只把它写进档案列、**不走核验端点**
#: ⇒ 不需要过本地校验；字形与 `tests/test_realname_verify_api.py` 的合成值同族。
SYNTH_ID_CARD = "110101199001011234"


class MyProfileRealNamePayloadTests(TenantTestCase):
    """T7 / spec §10.2：`GET /api/auth/profile` 回传本租户的实名开关与剩余次数。

    ⚠️ 判据一律跟**库里的行**对拍（E-0009 / E-0024）：把常量当唯一判据时，
    「两个读数写反了」「恒回同一对值」这两类坏法**照样绿**。
    """

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)  # 挡住上一条用例残留的 schema 上下文

    # ── 夹具 ────────────────────────────────────────────────────────────────
    def _profile(self, client):
        res = client.get("/api/auth/profile")
        self.assertEqual(res.status_code, 200, res.content[:200])
        return res.json()["data"]["profile"]

    def _row_values(self, setting_pk, grant_pk):
        """从库里**重读**那两行（⛔ 不拿刚 `create` 出来的内存对象当判据）。"""
        setting = RealNameSetting.objects.get(pk=setting_pk)
        grant = RealNameGrant.objects.get(pk=grant_pk)
        return {"enabled": setting.enabled, "remaining": grant.granted - grant.used}

    # ── ① 与库里的行一致（⛔ 不是常量）─────────────────────────────────────
    def test_00_payload_matches_the_setting_and_grant_rows(self):
        setting = RealNameSetting.objects.create(
            tenant_schema=connection.schema_name, enabled=True
        )
        grant = RealNameGrant.objects.create(tenant=self.tenant, granted=5, used=2)
        # 夹具自证：起点**刻意不是平凡值**（`enabled=False` / `used=0` 抓不到
        # 「用真值判断」与「减法写反」这两类坏法）。
        self.assertIs(RealNameSetting.objects.get(pk=setting.pk).enabled, True)
        self.assertEqual(
            (RealNameGrant.objects.get(pk=grant.pk).granted,
             RealNameGrant.objects.get(pk=grant.pk).used),
            (5, 2),
        )

        data = self._profile(make_logged_in_client(groups=("客服",)))["realname"]

        # 载荷**恰好**是这两个事实（多一个键就是新开了一个出口面）。
        self.assertEqual(sorted(data), ["enabled", "remaining"])
        self.assertEqual(data, self._row_values(setting.pk, grant.pk))
        # ⚠️ 同时钉**值**：起点的读数必须是 `True` / `3`（`granted - used`，
        #    ⛔ 不是 `granted + used` / `used` 之类）。
        self.assertIs(data["enabled"], True)
        self.assertEqual(data["remaining"], 3)

    def test_01_both_readings_follow_the_rows_when_they_change(self):
        """①的判别力：把库里两行改成**相反值** ⇒ 载荷必须跟着翻（⛔ 不是恒回同一对）。"""
        setting = RealNameSetting.objects.create(
            tenant_schema=connection.schema_name, enabled=True
        )
        grant = RealNameGrant.objects.create(tenant=self.tenant, granted=5, used=2)
        client = make_logged_in_client(groups=("客服",))
        self.assertEqual(self._profile(client)["realname"], self._row_values(setting.pk, grant.pk))

        RealNameSetting.objects.filter(pk=setting.pk).update(enabled=False)
        RealNameGrant.objects.filter(pk=grant.pk).update(used=5)

        after = self._profile(client)["realname"]
        self.assertEqual(after, self._row_values(setting.pk, grant.pk))
        self.assertIs(after["enabled"], False)
        # `5 - 5` ⇒ `0`：⚠️ `0` 是**合法读数**（用真值判断的实现会把它写成 `None`）。
        self.assertEqual(after["remaining"], 0)

    # ── ② 行都不存在 ────────────────────────────────────────────────────────
    def test_02_defaults_when_neither_row_exists(self):
        """没有设置行 / 没有额度行 ⇒ `enabled=False`、`remaining=0`（⛔ 不是 `None`）。

        ⚠️ `0` 与 `None` 必须能被客户端区分（`None` = **我们读不到**，见
        `apps/realname/views.py::remaining_quota` 的表）⇒ 这里两条一起钉。
        """
        self.assertFalse(RealNameSetting.objects.exists(), "起点应当是「没有设置行」")
        self.assertFalse(RealNameGrant.objects.filter(tenant=self.tenant).exists())

        data = self._profile(make_logged_in_client(groups=("客服",)))["realname"]

        self.assertIs(data["enabled"], False)   # 模型列自己的默认值（⛔ 不是"读不到"）
        self.assertIsNotNone(data["remaining"], "读不到才会是 None，这一档是「没买过」⇒ 0")
        self.assertEqual(data["remaining"], 0)

    # ── ④ ⭐ `is_real_name` 的语义一字未改（spec §2-B4 / §15 的「不阻断」）────
    def test_03_is_real_name_still_only_reads_the_profile_column(self):
        """⭐ `is_real_name` 仍**只**看档案上有没有号：与"功能开没开 / 核没核过"**无关**。

        ⚠️ 这正是本线要钉住的「**不阻断**任何既有操作」：管理员**手填**的号也算"已实名"
        （spec §10.3 / §14-P9 的残差如实登记）⇒ 新加的 `realname` 子结构⛔ **不许**
        反过来把 `is_real_name` 改成"必须核验过 / 必须启用"。
        """
        # 开关**开着**，但一次都没核验过 —— 这样下面两条都不是"被开关顺带决定的"。
        RealNameSetting.objects.create(tenant_schema=connection.schema_name, enabled=True)
        # `games=None` = 不建档案（夹具语义见 `p5_auth_helpers`）⇒ 本用例自己造两态。
        client = make_logged_in_client(groups=("客服",), games=None)
        user = get_user_model().objects.get(username=P2_TEST_USERNAME)
        profile = StaffProfile.objects.create(user=user, id_card_no="")

        data = self._profile(client)
        self.assertEqual(data["id_card_no"], "")
        self.assertIs(data["is_real_name"], False)          # 开关开着 ≠ 已实名
        self.assertIs(data["realname"]["enabled"], True)    # 两个事实各论各的

        # 填上号 —— **不经过核验端点、不写任何核验流水**（既有路径：管理员手填，spec §14-P9）。
        StaffProfile.objects.filter(pk=profile.pk).update(id_card_no=SYNTH_ID_CARD)
        self.assertEqual(RealNameCertRecord.objects.count(), 0, "本用例不该产生核验流水")

        data = self._profile(client)
        self.assertEqual(data["id_card_no"], SYNTH_ID_CARD)
        self.assertIs(data["is_real_name"], True)           # 仍是 `bool(profile.id_card_no)`
        self.assertEqual(RealNameCertRecord.objects.count(), 0)
