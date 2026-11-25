"""企微群码自动上传（锁定应用）线的**模型级**判据（T1）。

设计文档：`docs/specs/2026-10-09-wecom-group-qr-auto-upload-design.md` §5.1 / §4-D1 / §4-D2。

本文件（T1）只有三条，逐条对应 spec 里的一句口径：

1. `test_second_wecom_app_in_the_same_tenant_is_refused_by_the_database`
   —— **§4-D2**：「每租户唯一」落成**库级**判据。判据必须是**数据库**拒绝
   （`IntegrityError`），⛔ 不是"应用层 `exists()` 检查"那种并发下会被穿过的写法；
   所以用例里**不碰任何服务层 / 视图**，直接对模型插第二行。
2. `test_existing_rows_default_to_general` —— **§5.1**：不传 `kind` 建出来的行取值是
   `general`（既有行吃 `default=general` ⇒ 零变化），且**通用这一档可以有很多条**
   （条件索引只把 `wecom_group_qr` 那一档纳入）。
3. `test_two_wecom_apps_are_allowed_in_different_tenants` —— **§4-D2 括号里那句**
   「表在租户 schema 内 ⇒ 天然是每租户」：A / B 两个租户**各自**建一把锁定应用，**都成功**。

⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名用 `…test.invalid`、
appKey / appSecret 用 `synthetic` 形状的串，⛔ 与任何真实租户或真实密钥无关。
⚠️ 断言打在**数据库行**上（`objects.get(pk=…)` 读回来），⛔ 不看类属性默认值
—— `objects.create()` 不吃类属性默认值，只看类属性的判据抓不到"类上写着 default、库里是空值"。
⚠️ 业务值写**字面量**（`"general"` / `"wecom_group_qr"`），⛔ 不从 `ApiAppKind` import
（E-0024：钉值不钉常量 —— 常量被改坏时自指断言不会红）。
"""
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.openapi.models import ApiApp
from apps.platform.provisioning import provision_tenant

User = get_user_model()

#: 租户 B 的域名与 schema 名（与 `TenantTestCase` 自带的租户 A 区分开）。
#: ⚠️ 域名刻意用 `test.invalid`：`Domain` 上有唯一约束，撞名会让 `provision_tenant` 报错。
#: 与 `tests/test_openapi_isolation.py` 的取值**不同**（各文件各用一套，互不干扰）。
DOMAIN_B = "wecom-gqr-b.test.invalid"
SCHEMA_B = "wecom_gqr_b"


class ApiAppKindModelTests(TenantTestCase):
    """`ApiApp.kind` 列 + `uniq_apiapp_wecom_qr` 条件唯一约束。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 第二个租户必须在 `setUpClass` 里建：schema 的 DDL 不能回滚在 `TestCase` 的事务里
        # （与 `tests/test_openapi_isolation.py` / `tests/test_account_isolation.py` 同一手法）。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="企微群码测试租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行把连接钉回租户 A：上一条用例里 `tenant_context(tenant_b)` 退出时会切回来，
        # 但这里再显式钉一次，挡的是用例之间的残留（同 `test_openapi_isolation.py` 的口径）。
        connection.set_tenant(self.tenant)
        super().setUp()

    # ── 1. 同租户第二把锁定应用 ⇒ 库拒 ────────────────────────────────
    def test_second_wecom_app_in_the_same_tenant_is_refused_by_the_database(self):
        """⭐ **这条是 `uniq_apiapp_wecom_qr` 条件唯一约束的判据**（§4-D2）：同租户第二把 ⇒ `IntegrityError`。

        ⚠️ **先自证第一行真的落库了**（按 `kind` 查到、且恰好一行）：没有这两句，
        一个"根本什么都没写进去"的实现也会让 `IntegrityError` 看着像"约束生效"
        （同 `tests/test_openapi_model.py` 的 `app_key` 那条）。
        ⚠️ 严格区分「**数据库**拒绝」与「**应用层**检查」：本用例只走 `objects.create()`，
        没有任何服务层 / 视图 / 序列化器参与 ⇒ 绿了就说明**约束本身**在库上（那是 D2 的判据）；
        若把约束删掉、只在服务层加一句 `if exists(): raise`，本用例会以
        `IntegrityError not raised` **红**。
        """
        first = self._app(
            name="企微群码锁定应用甲",
            app_key="synthetic-wecom-key-0001",
            kind="wecom_group_qr",
        )
        # 自证：第一行**真的**在库里，且这一档此刻恰好一行
        self.assertEqual(
            ApiApp.objects.filter(kind="wecom_group_qr").count(), 1
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._app(
                    name="企微群码锁定应用乙",
                    app_key="synthetic-wecom-key-0002",
                    kind="wecom_group_qr",
                )

        # 撞库之后原来那一行还在（约束是"拒第二条"，⛔ 不是"清表"）
        self.assertEqual(
            ApiApp.objects.filter(kind="wecom_group_qr").count(), 1
        )
        self.assertEqual(
            ApiApp.objects.get(pk=first.pk).app_key, "synthetic-wecom-key-0001"
        )

    # ── 2. 默认值与「通用档不受约束」 ─────────────────────────────────
    def test_existing_rows_default_to_general(self):
        """不传 `kind` ⇒ 库里那一行是 `general`；**第二行 general 也建得出来**（§5.1）。

        这条守两件事：
        ① **默认值落在库里**（既有行吃 `default=general` ⇒ 零变化）——判据读的是
           `ApiApp.objects.get(pk=…).kind`，⛔ 不是类属性；
        ② **条件索引不约束 `general`**：`condition=Q(kind="wecom_group_qr")` 只把锁定那一档
           纳入索引 ⇒ 通用行想建多少条都行。判别力：若把 `condition=` 写掉（变成对 `kind`
           整列的普通唯一约束），第一行 general 建得出来、**第二行会被拒** ⇒ 这条当场红。
        """
        first = self._app(name="通用应用甲", app_key="synthetic-general-key-001")
        second = self._app(name="通用应用乙", app_key="synthetic-general-key-002")

        # ① 两行都**读回来**确认：默认值是 `general`（钉字面值，⛔ 不 import 常量）
        self.assertEqual(ApiApp.objects.get(pk=first.pk).kind, "general")
        self.assertEqual(ApiApp.objects.get(pk=second.pk).kind, "general")
        # ② 通用档**有两行**（⛔ 不是"只允许一行"）
        self.assertEqual(ApiApp.objects.filter(kind="general").count(), 2)

    # ── 3. 不同租户各一把 ⇒ 都成功 ───────────────────────────────────
    def test_two_wecom_apps_are_allowed_in_different_tenants(self):
        """A 租户一把、B 租户一把 ⇒ **都成功**（§4-D2：「表在租户 schema 内 ⇒ 天然每租户」）。

        形状：两个租户里各建**恰好一把**锁定应用，两边都不抛异常；再各自读回自己那一行
        （判据用**租户内唯一、且两租户取值不同**的字段 `app_key`，⛔ 不用主键 —— 两个 schema
        的主键序列各自从 1 开始，按主键断言恒真或恒假，`AGENTS.md` §5-G 明文）。

        ⚠️ 判别力：约束若写成**不带 `condition` 的整列唯一**，或者被做成了"全库一把"的形态，
        第二条（B 那一把）会以 `IntegrityError` **红** ⇒ 正是"每租户一把"与"全局一把"的分界。
        """
        # 租户 A（`TenantTestCase` 自带的 schema）
        app_a = self._app(
            name="甲租户锁定应用",
            app_key="synthetic-wecom-a-0001",
            kind="wecom_group_qr",
        )
        self.assertEqual(ApiApp.objects.get(pk=app_a.pk).kind, "wecom_group_qr")

        # 租户 B：建第二把**同样** `kind=wecom_group_qr` 的行 ⇒ ⛔ 必须成功
        try:
            with tenant_context(self.tenant_b):
                app_b = self._app(
                    name="乙租户锁定应用",
                    app_key="synthetic-wecom-b-0001",
                    kind="wecom_group_qr",
                )
                # B 里那一行**读得回来**，且就是刚建的那一行（用 app_key 指认，⛔ 不用主键）
                self.assertEqual(
                    ApiApp.objects.get(app_key="synthetic-wecom-b-0001").kind,
                    "wecom_group_qr",
                )
                # B 里这一档恰好一行（B 里没有 A 那一行）
                self.assertEqual(
                    ApiApp.objects.filter(kind="wecom_group_qr").count(), 1
                )
                self.assertNotEqual(app_b.app_key, app_a.app_key)
        finally:
            connection.set_tenant(self.tenant)

        # A 侧读数不受 B 影响：两边各一行、各自的那一行都在
        self.assertEqual(ApiApp.objects.filter(kind="wecom_group_qr").count(), 1)
        self.assertEqual(
            ApiApp.objects.get(app_key="synthetic-wecom-a-0001").pk, app_a.pk
        )
        self.assertEqual(
            ApiApp.objects.filter(app_key="synthetic-wecom-b-0001").count(), 0
        )

    # ── 工具 ─────────────────────────────────────────────────────────
    def _app(self, *, name, app_key, kind=None):
        """在**当前 schema** 里建一个应用。

        `kind=None` ⇒ **不传这个参数**（走列默认值，第 2 条用例要的形态）；给了就传进去。
        ⚠️ 创建人按用户名 `get_or_create`（⛔ 不预设密码哈希之外的任何东西；口令串是合成值）。
        """
        creator, _ = User.objects.get_or_create(
            username="openapi-wecom-qr-creator",
            defaults={"password": "unused-in-this-test"},
        )
        kwargs = {} if kind is None else {"kind": kind}
        return ApiApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret="synthetic-secret-not-a-real-key",
            created_by=creator,
            **kwargs,
        )
