"""`ApiApp` 的模型级判据（spec `docs/specs/2026-10-07-open-platform-api-design.md` §5.1 / §5.4 / D10）。

这一份守的是四类**只在模型层才能钉住**的事：

1. **`app_key` 的唯一索引真的在数据库上**（不是只有 `unique=True` 那句声明）——它是 §5.4
   「按 key 查一行、只对那一行验签」的承重点，掉了它功能上看不出异常、只有量级变化；
2. ~~**两张 M2M（`nodes` / `games`）能存能读**~~：**这一整组（5 条）已在 T10 同一次删除**
   （设计文档 §13.1）—— 那三列（两个 M2M + 一个「全部游戏」布尔）随 v1 面删除。
   原文描述的判据：「两张 M2M 能存能读 / `games` 空集 = 空集 / `all_games` 默认 False /
   它与 M2M 是两个独立载体」。⚠️ 逐条理由见下面 `test_openapi_model.py` 里那段改准块。

3. **`created_by` 的 `PROTECT` 真的会挡住删那个 `User`**（删被引用者必须抛 `ProtectedError`）；
4. ⭐ **D10 的"取消专用账号"是被钉死的**：`ApiApp` 上**不许**出现 `service_account`
   （不是"现在没用它"，是**字段不存在**）。

⚠️ **只增（T1）**：上面 1–5 组的**用例一条未动**（本文件只改了这一行的说明）。
   v2 那批模型改动（`ApiApp` 加两列 + 三张新表）的判据**全部落在下面的 `OpenApiV2ModelTests`** 里 ——
   ⛔ 不回改上面的用例（`AGENTS.md` §4 的只增不改口径）。

⚠️ 断言全部打在**数据库行 / `_meta`** 这些真实出口上，不打 logger、不看 `response.data` 是否为 None
（E-0063）；字段名与状态值写**字面量**，不从常量模块 import（E-0024：钉值不钉常量）。
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.openapi.models import (
    ApiApp,
    OpenApiDelivery,
    OpenApiIdempotency,
    OpenApiRateBucket,
)


class ApiAppModelTests(TenantTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="openapi-creator", password="pw-123456"
        )

    def _app(self, name="测试应用甲", app_key="testkey00000000a", **kwargs):
        return ApiApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret="testsecret00000000a",
            created_by=self.user,
            **kwargs,
        )

    # ── 1. `app_key` / `name` 的唯一约束 ─────────────────────────────
    def test_duplicate_app_key_is_rejected(self):
        """同一个 `app_key` 第二次入库必须被数据库拒绝（§5.4 的唯一索引是承重的）。

        ⚠️ 只断言「ORM 抛 `IntegrityError`」不够——那句话在**没有唯一索引**时也会绿，
        因为 `full_clean()` 不参与 `objects.create()`。所以这里**先确认第一行真的落库了**
        （按 key 能查到、且只有一行），再让第二次撞库。
        """
        self._app(name="测试应用甲", app_key="dupkey000000000a")
        self.assertEqual(ApiApp.objects.filter(app_key="dupkey000000000a").count(), 1)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._app(name="测试应用乙", app_key="dupkey000000000a")

    def test_duplicate_name_is_rejected(self):
        """应用名在**租户内唯一**（人工指认用，§5.1）。"""
        self._app(name="重名应用", app_key="namekey00000000a")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._app(name="重名应用", app_key="namekey00000000b")

    def test_app_key_lookup_returns_exactly_one_row(self):
        """§5.4 的查找形态：按 `app_key` **一次查询命中一行**（不是逐行扫哈希）。"""
        created = self._app(name="可查应用", app_key="lookupkey000000a")
        self._app(name="另一个应用", app_key="lookupkey000000b")
        hit = ApiApp.objects.filter(app_key="lookupkey000000a").first()
        self.assertIsNotNone(hit)
        self.assertEqual(hit.pk, created.pk)

    # ── 2. 授权范围列已不存在────────────────────────────
# ⛔ 此处原有 5 条 M2M / "全部游戏" 用例（存读、空集语义、fail-closed 默认、
# 双载体对照），那三列随 v1 面删除（§4-V2：一个应用凭据 = 本租户全部业务数据）
# ⇒ 今天 `ApiApp` 上**没有任何授权范围列**。丢掉的判别力：③④⑤ 钉的
# "fail-closed / 不快照"授权面安全口径随授权面一起消失。
# ⚠️ 本文件第 1 / 3 / 4 / 5 组（唯一索引 / `state` 默认值 / `created_by` 的 `PROTECT` /
# D10 无 `service_account`）**一条未动**。

    # ── 3. `state` 默认值 ──────────────────────────────────────────
    def test_state_defaults_to_enabled(self):
        app = self._app(name="默认状态应用", app_key="statekey0000000a")
        self.assertEqual(app.state, "enabled")           # 钉字面值，不写 ApiAppState.ENABLED（E-0024）
        self.assertEqual(ApiApp.objects.get(pk=app.pk).state, "enabled")

    # ── 4. `created_by` 的 PROTECT ─────────────────────────────────
    def test_created_by_protects_the_user_from_deletion(self):
        """删「创建过应用」的账号必须被数据库挡住（`on_delete=PROTECT`）。

        ⚠️ 这是**字段级**判据，不是"只停用不能删"那条（§15.2 已说明：`PROTECT` 保护的是
        **被引用的那一行**即 `User`，与应用自己能不能删无关），⛔ 别混。
        """
        app = self._app(name="有创建人的应用", app_key="protectkey000000")
        with self.assertRaises(ProtectedError):
            self.user.delete()
        self.assertTrue(ApiApp.objects.filter(pk=app.pk).exists())
        self.assertTrue(
            get_user_model().objects.filter(username="openapi-creator").exists()
        )

    def test_user_without_apps_can_still_be_deleted(self):
        """对照：没建过应用的账号**照常可删**——否则上一条可能是"谁都删不掉"造成的假绿。"""
        other = get_user_model().objects.create_user(username="no-app-user", password="pw-123456")
        other.delete()
        self.assertFalse(get_user_model().objects.filter(username="no-app-user").exists())

    # ── 6. ⛔⭐ T10：授权范围那三列不许回来（收口任务的删除面）──────
    def test_the_authorization_scope_columns_are_gone(self):
        """⛔⭐ T10（2026-10-08）：`ApiApp` 的**列恰好**是下面这一组 —— 授权范围那三列不许回来。

        ## 为什么要有这一条（⛔ 不是"顺手加个字段清单"）

        收口任务删掉的是**授权范围**这一整块（设计文档第 13.1 节）：权限节点 M2M、
        游戏 M2M、以及「全部游戏」那个布尔。删它们的依据是一条**承诺** ——
        「一个应用凭据 = 本租户全部业务数据」（§4-V2）。

        ⚠️ **端点的行为判据看不出这三列回来**：它们可以只躺在库里、不接在任何查询上
        （"配了但不生效"），于是所有端点用例照样绿。⇒ 这一条是本任务唯一能挡住
        "授权面悄悄长回来"的判据，形状照上面 D10 那条（同族：某个字段**不存在**）。

        ⚠️ 判据取**恰好集合**（⛔ 不是"不包含那三个名字"）：后者能被"换个名字加回来"绕过，
        而恰好集合会逼人当场说清"这一列是什么、谁写的、留痕在哪"。
        ⚠️ 判据取 `local_fields` **加上** `local_many_to_many`（⛔ 不是 `concrete_fields`）：
        后者按 Django 自己的定义**不含 M2M**（它们没有列、住在中间表里）
        —— ⛔⭐ 这一点是**弄红时实测发现的**：只写 `concrete_fields` 时，把那个节点 M2M
        加回来**这条等号照样绿**（判别力只剩下面那个 `FieldDoesNotExist` 循环）。
        `local_fields` + `local_many_to_many` 才是"本表自己声明的东西"的**恰好集合**
        （不含反向关联与被继承的字段）。

        `kind` 在这张恰好集合里：同一张 `ApiApp` 表用它区分「企微群码上传专用」和「通用」，
        不是另一张表，也不是本条要挡的授权范围那三列。
        """
        declared = set(ApiApp._meta.local_fields) | set(ApiApp._meta.local_many_to_many)
        self.assertEqual(
            sorted(f.name for f in declared),
            [
                "app_key", "app_secret", "callback_state", "callback_url",
                "created_at", "created_by", "id", "ip_allowlist", "kind",
                "last_used_at", "name", "secret_rotated_at", "state",
            ],
        )
        for gone in ("nodes", "games", "all_games"):
            with self.subTest(field=gone):
                with self.assertRaises(FieldDoesNotExist):
                    ApiApp._meta.get_field(gone)

    # ── 5. D10：不许有 `service_account` ───────────────────────────
    def test_no_service_account_field(self):
        """⛔ D10（2026-10-07 业务方改定）：应用**不挂任何账号** ⇒ 这个字段不许存在。

        判据打在 `_meta.get_field` 上（而不是 `hasattr`）：后者对"字段已删但类上还留了个
        property / 类属性"会返回真，那正是本用例要挡的形状。
        """
        with self.assertRaises(FieldDoesNotExist):
            ApiApp._meta.get_field("service_account")
        field_names = {f.name for f in ApiApp._meta.get_fields()}
        self.assertNotIn("service_account", field_names)
        self.assertNotIn("account", field_names)
        self.assertNotIn("user", field_names)
        # 反向确认：挂账号的那条路若回来了，一定会带一个指向 AUTH_USER_MODEL 的 FK
        # ⇒ 除 `created_by` 外不许有第二个指向用户的关联。
        user_links = [
            f.name
            for f in ApiApp._meta.get_fields()
            if getattr(f, "related_model", None) is get_user_model()
        ]
        self.assertEqual(user_links, ["created_by"])

    def test_no_tenant_id_column(self):
        """⛔ 不带 `tenant_id`：本表在每个租户 schema 各一份（与 `apps.business` 同口径）。"""
        field_names = {f.name for f in ApiApp._meta.get_fields()}
        self.assertNotIn("tenant_id", field_names)
        self.assertNotIn("tenant", field_names)

    def test_the_models_in_this_app_are_exactly_these(self):
        """本 app 的模型名恰好是已登记的五个。

        ## ⚠️ 旧用例原文（⛔ 原样保留，`AGENTS.md` §4「只增不改」）

        改准前的用例名与正文**逐字**如下（出处：**旧 spec §5.1 末尾**，
        `docs/specs/2026-10-07-open-platform-api-design.md`）：

            def test_no_rate_limit_or_nonce_tables_in_this_app(self):
                \"\"\"⛔ §5.1 末尾：**不建**限流计数表、**不建** nonce 表 ⇒ 本 app 只有一个模型。

                判据打在 app registry 上（不是"我没写"）：模型数变了就红，逼后来者在 spec 里补一行。
                \"\"\"
                from django.apps import apps as django_apps

                model_names = sorted(
                    m.__name__ for m in django_apps.get_app_config("openapi").get_models()
                )
                self.assertEqual(model_names, ["ApiApp"])

        限流计数由数据库共享，打手绑定保存业务应用的外部账号对应关系。
        防重放模型另由 test_no_nonce_table_in_this_app 检查。
        """
        from django.apps import apps as django_apps

        model_names = sorted(
            m.__name__ for m in django_apps.get_app_config("openapi").get_models()
        )
        self.assertEqual(
            model_names,
            [
                "ApiApp",
                "IntegrationTakerBinding",
                "OpenApiDelivery",
                "OpenApiIdempotency",
                "OpenApiRateBucket",
            ],
        )

    def test_no_nonce_table_in_this_app(self):
        """⛔ **本 app 里没有任何承担 nonce / 防重放随机数职责的表**（旧钉子 ② 的另一半，仍然成立）。

        ## 为什么这一半今天还成立

        v2 的令牌是**无状态自签**（§5.3：`HMAC(app_secret, payload)` + `exp`，⛔ 不入库令牌、⛔ 不落随机数）；
        写端点靠**幂等键**（§6.3）而不是 nonce ⇒ 旧句里的「不建 nonce 表」**一个字没变**。

        ⚠️ **`OpenApiIdempotency` 不是 nonce 表**（这两个词常被混用，所以在这里说破）：
        它是**幂等键台账** —— 同一个键 + **同一份请求体**（`request_hash`）⇒ 回放同一个响应快照；
        ⛔ 它**不做**时间窗随机数校验、⛔ **不参与**令牌校验、⛔ 不消费一次性随机数。
        「一次性」与「可重放」是相反的两件事：nonce 用过即废，幂等键**本来就是要被重复使用的**。

        ## 判据形态（⛔ 为什么不是"模型名里不含 nonce"）

        按**名字**判是假判据（改个名就绕过去了）。这里按**列名**判：本 app 全部模型的字段名里
        ⛔ 不许出现 `nonce` / `replay` / `random` 这类字面 —— 一张换了名字的 nonce 表**仍然要有个列
        装那个随机数**，所以它照样红。

        ⚠️ **它比上一条多出来的判别力（有了它上一条才不是唯一一道门）**：从上面那条的期望集合里
        删掉一个**新造的表名**（`OpenApiIdempotency`）时，上面那条红而**这条不红**
        （剩下的模型列名一个都不含那三个字面）⇒ 两条是各自独立的门，⛔ 不是同一条的复述。
        """
        from django.apps import apps as django_apps

        forbidden = ("nonce", "replay", "random")
        hits = {}
        for model in django_apps.get_app_config("openapi").get_models():
            field_names = [f.name for f in model._meta.get_fields()]
            matched = [n for n in field_names if any(t in n.lower() for t in forbidden)]
            if matched:
                hits[model.__name__] = matched
        self.assertEqual(
            hits,
            {},
            "本 app 出现了承担 nonce / 防重放随机数职责的列 ⇒ v2 的令牌是无状态自签（§5.3）、"
            f"写端点用幂等键而非 nonce（§6.3）；命中的模型与列：{hits}",
        )
        # 反向自证：`hits` 是"循环里一个字段都没扫到"也能得到的空字典 ⇒ 先证明真的枚举到了模型，
        # 否则上面那条会**恒真**（`AGENTS.md` §5-G：结构性断言常见这个"空集合"空洞）。
        self.assertEqual(
            len(list(django_apps.get_app_config("openapi").get_models())), 5
        )


class OpenApiV2ModelTests(TenantTestCase):
    """v2 那批**只增**的模型改动（spec `docs/specs/2026-10-08-open-platform-v2-parity-design.md`
    §7.1 / §6.3 / §10；T1）。

    守四类事：

    1. `ApiApp` 两个新列的**默认值**（断言打在**数据库行**上 —— `objects.create()` 不吃类属性默认值，
       所以"类上写着 default 但库里是空值 / 别的值"这种形状只有读回来才抓得到）；
    2. `OpenApiDelivery` 能建能读、状态默认值符合定义；
    3. ⛔ **两条唯一约束真的会拦**（`OpenApiIdempotency` 的 `(app, key)`、
       `OpenApiRateBucket` 的 `(scope, ident, window_start)`）—— 「ORM 抛 `IntegrityError`」这句话在
       **没有**唯一约束时也会绿，所以两条都先**确认第一行真的落库了**，再让第二行撞库；
       ⭐ 桶那条另配**对照**（只把 `scope` 换成 `ip` ⇒ **允许**）证明 `scope` 真的进了唯一键、不是摆设；
    4. 桶的 `count` 初值与"两种 scope 各自独立计行"的形态。

    ⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C），⛔ 与任何真实租户 / 真实业务数据无关。
    ⛔ 本类**不**测 https / SSRF 校验与限流阈值：那些是 T3 / T6 的逻辑，T1 只加列与注释。
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="openapi-v2-creator", password="pw-123456"
        )

    def _app(self, name="测试应用甲", app_key="v2key000000000a", **kwargs):
        return ApiApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret="testsecret00000000a",
            created_by=self.user,
            **kwargs,
        )

    # ── 1. 两个新列的默认值（判据打在库里的那一行上） ──────────────
    def test_callback_url_defaults_to_empty_string_in_the_database_row(self):
        """`callback_url` 的库里默认值是**空串**（§7.1：地址为空 ⇒ 不产生投递）。

        判别力：默认值若被写成占位地址（"先给一个默认回调"的一种误读），
        这条当场红；而只断言类属性时，`objects.create()` 的实参能把它盖过去、看不出来。
        """
        app = self._app(name="回调默认应用", app_key="t1callbackurl0a")
        self.assertEqual(ApiApp.objects.get(pk=app.pk).callback_url, "")   # 库里的那一行

    def test_callback_state_defaults_to_enabled_in_the_database_row(self):
        """`callback_state` 的库里默认值是 `enabled`（§7.1）。

        ⚠️ 这是**有意选的默认值**（理由见 `apps/openapi/models.py` 的列注释：地址为空本来就不投递
        ⇒ 默认值不改变安全面），所以这里钉的是 `enabled` 而**不是** fail-closed 的 `disabled`。
        判据打在**库里的那一行**上：⛔ 不看类属性的默认值。
        """
        app = self._app(name="回调开关默认应用", app_key="t1callbackstate")
        self.assertEqual(ApiApp.objects.get(pk=app.pk).callback_state, "enabled")

    # ── 2. `OpenApiDelivery` 能建能读 ──────────────────────────────
    def test_delivery_row_can_be_created_and_read_back(self):
        """outbox 台账能建一行、读回来四个初值都对（§7.4）。

        注意 `payload` 里**不含签名**（签名在投递时用当时的密钥现算，理由见模型 docstring）
        —— 这条只把"报文主体就是写进去的那份"钉住，⛔ 不在这里断言签名。
        """
        app = self._app(name="推送台账应用", app_key="t1deliverykey00")
        payload = {"event": "order.status_changed", "order_no": "TT-OPV2-0001", "status": "代练中"}
        row = OpenApiDelivery.objects.create(
            app=app,
            event="order.status_changed",
            order_no="TT-OPV2-0001",
            source_order_no="",
            payload=payload,
        )

        reread = OpenApiDelivery.objects.get(pk=row.pk)
        self.assertEqual(reread.app_id, app.pk)
        self.assertEqual(reread.event, "order.status_changed")
        self.assertEqual(reread.order_no, "TT-OPV2-0001")
        self.assertEqual(reread.payload, payload)
        # 默认值逐条钉字面量（⛔ 不 import `OpenApiDelivery.Status.PENDING`：E-0024 钉值不钉常量）
        self.assertEqual(reread.status, "pending")
        self.assertEqual(reread.attempts, 0)
        self.assertEqual(reread.reason, "")
        self.assertIsNone(reread.next_attempt_at)
        self.assertIsNone(reread.response_status)
        self.assertIsNone(reread.processed_at)
        self.assertIsNotNone(reread.created_at)

    # ── 3. 幂等键的唯一约束 ────────────────────────────────────────
    def test_idempotency_duplicate_key_for_same_app_is_rejected(self):
        """⭐ **这条是 `(app, key)` 唯一约束的判据**（§6.3）：同一应用同一键插第二行 ⇒ `IntegrityError`。

        ⚠️ **先把回放用的第一行抓到手里**再撞库：没有那两句，一条"什么都没写进去"的实现
        （例如键被静默丢弃）也会让 `IntegrityError` 看着像"约束生效"，其实第二行根本没写过。
        """
        app = self._app(name="幂等约束应用", app_key="t1idemkey00000a")
        payload = dict(
            method="POST",
            path="/api/open/v1/orders/",
            request_hash="0" * 64,
            response_snapshot={"code": 0, "data": {"order_no": "TT-OPV2-0002"}},
            status_code=200,
        )
        first = OpenApiIdempotency.objects.create(app=app, key="t1-idem-key-0001", **payload)
        self.assertEqual(
            OpenApiIdempotency.objects.filter(app=app, key="t1-idem-key-0001").count(), 1
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OpenApiIdempotency.objects.create(app=app, key="t1-idem-key-0001", **payload)
        # 撞库之后原来那一行还在（约束是"拒第二条"，⛔ 不是"清表"）
        self.assertEqual(OpenApiIdempotency.objects.get(pk=first.pk).status_code, 200)

    def test_idempotency_same_key_under_another_app_is_allowed(self):
        """对照：唯一键是 **`(app, key)`**、⛔ 不是 `key` 全局唯一 —— 两个应用撞同一个键**合法**。

        判据的作用：若给 `key` 加了 `unique=True`（"键全局唯一"的一种误读），这条会红；
        而上面那条撞库用例**照样绿** ⇒ 两条合起来才钉住"唯一性是按应用分的"。
        """
        app_a = self._app(name="幂等应用甲", app_key="t1idemapp0000aa")
        app_b = self._app(name="幂等应用乙", app_key="t1idemapp0000bb")
        common = dict(
            method="POST",
            path="/api/open/v1/orders/",
            request_hash="1" * 64,
            response_snapshot={"code": 0, "data": None},
            status_code=200,
        )
        OpenApiIdempotency.objects.create(app=app_a, key="t1-shared-key-001", **common)
        OpenApiIdempotency.objects.create(app=app_b, key="t1-shared-key-001", **common)
        self.assertEqual(OpenApiIdempotency.objects.filter(key="t1-shared-key-001").count(), 2)

    # ── 4. 限流桶的唯一约束 + `scope` 进唯一键的对照 ────────────────
    def test_rate_bucket_duplicate_scope_ident_window_is_rejected(self):
        """⭐ **这条是 `(scope, ident, window_start)` 唯一约束的判据**（§10）：同三列插第二行 ⇒ `IntegrityError`。

        先自证第一行真的落库（同幂等那条的理由）：没了那两句，一个"根本没写进去"的实现也会看着像绿。
        ⚠️ **第二次插入必须复用同一个 `window_start` 值**（先取进变量再传两遍）：
        两边各调一次 `timezone.now()` 会拿到**微秒不同**的两个时刻 ⇒ 那是两行**合法**的窗口，
        约束不该拦，而用例会以 `IntegrityError not raised` **假红**
        （本用例第一版就是这么写的，实测踩到）。
        """
        window = self._window()
        bucket = OpenApiRateBucket.objects.create(
            scope="app", ident="t1-bucket-appkey-01", window_start=window, count=1
        )
        self.assertEqual(OpenApiRateBucket.objects.filter(pk=bucket.pk).count(), 1)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OpenApiRateBucket.objects.create(
                    scope="app",
                    ident="t1-bucket-appkey-01",
                    window_start=window,          # ⚠️ 同一个时刻（⛔ 不重新 now()）
                    count=1,
                )
        self.assertEqual(OpenApiRateBucket.objects.get(pk=bucket.pk).count, 1)

    def test_rate_bucket_same_three_values_under_ip_scope_is_allowed(self):
        """⭐ **对照**：只把 `scope` 换成 `ip`、其余三列**逐字相同** ⇒ **允许**插进去。

        这条证的是 **`scope` 真的进了唯一键**（⛔ 不是摆设）：少了它，同 `ident` / 同窗口的第二行
        会被上面那条约束挡住，而这条对照当场红。
        ⚠️ 它**不是**在说"两档计数合并"：`app` 档的额度与 `ip` 档的额度是**两个独立阈值**
        （§10 / §4-V4；表的形状与阈值的语义是两件事，见模型 docstring）。
        """
        window = self._window()
        ident_text = "t1-bucket-shared-01"       # 两行**故意**用同一个文本，才分辨得出 scope 的贡献
        OpenApiRateBucket.objects.create(scope="app", ident=ident_text, window_start=window, count=7)
        OpenApiRateBucket.objects.create(scope="ip", ident=ident_text, window_start=window, count=1)

        self.assertEqual(
            OpenApiRateBucket.objects.filter(ident=ident_text, window_start=window).count(), 2
        )
        # 两行的计数各自独立（⛔ 不是把两个桶的数加起来）
        self.assertEqual(
            OpenApiRateBucket.objects.get(scope="app", ident=ident_text).count, 7
        )
        self.assertEqual(
            OpenApiRateBucket.objects.get(scope="ip", ident=ident_text).count, 1
        )

    def test_rate_bucket_count_starts_at_zero(self):
        """`count` 初值 = 0（§10 的固定窗口：先建行、再 UPSERT 累加）。"""
        bucket = OpenApiRateBucket.objects.create(
            scope="ip", ident="203.0.113.7", window_start=self._window()
        )
        self.assertEqual(OpenApiRateBucket.objects.get(pk=bucket.pk).count, 0)

    @staticmethod
    def _window():
        """固定窗口的左端。⚠️ 值本身无语义，但**同一个用例里两次调用必须相等** ⇒ 先取好再传。"""
        return timezone.now()
