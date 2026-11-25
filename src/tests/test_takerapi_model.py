"""`apps.takerapi` 四张表的**模型级判据**（第一期 spec `docs/specs/2026-10-08-taker-open-api-design.md`
§3.1 / §3.2 / §3.3 ＋ 第二期 spec `docs/specs/2026-10-09-taker-callback-design.md` §3.1 / §3.2）。

⚠️ 形状照 `tests/test_openapi_model.py`（那是对应 app 的同族网），**⛔ 不是复制它**：
被测模型、列清单、约束名、弄红方式全部按本线自己的 spec 重写。

这一份守的是**只在模型层才钉得住**的几类事（每一条都对应 spec 的一行）：

1. **`TakerApp` 的列恰好是第一期的 10 列 ＋ 第二期回调那 2 列 ＋ `id`（共 13 项）**——
   ⛔ 多一列少一列都红。判别力所在：⛔ 不许有权限 / 游戏 M2M（范围从 `taker` 那一行算）、
   ⛔ 不许有租户列。⚠️ "恰好集合"是**有意**比"不包含那几个名字"强：后者能被"换个名字加回来"绕过。
   ⚠️⭐ **`callback_url` / `callback_state` 从"不许有"翻成"必须逐字有"**（第二期 T2）：
   这条网与 `test_the_forbidden_columns_do_not_exist` 是**一对**（那条只剩"不许有哪些"），
   两条一起才既挡住"漏加"、又挡住"多加"。
2. **本 app 的模型名恰好是四个**（第二期多一张 `TakerApiDelivery` ⇒ 当场红，
   逼人回 spec 补一行）。
3. **`TakerApiDelivery`（打手侧的 outbox + 台账）的列恰好是第二期 spec §3.2 那 12 列**
   ＋ `id`，`Event` 取值恰好四个（含本线独有的 `order.reassigned`）、`Status` 恰好三个。
4. **`taker` 是必填、且 `PROTECT`**（spec §3.1：范围判据的唯一来源；删掉有应用的账号会让范围失去依据）。
5. **`app_secret` 存库可读**（spec §3.1 / §3.2：验签要用它算 HMAC ⇒ 哈希了就验不了）——
   这条是**正面**判据（读回来等于写进去的明文），⛔ 不是"不许出现该字段"。
6. ⭐ **三条唯一约束真的在数据库上**（`TakerApp.app_key` / `(app, key)` / `(scope, ident, window_start)`）：
   它们都是**承重**的（按 key 一次索引查一行、幂等回放、固定窗口计数），掉了功能上看不出异常、
   只有量级或并发行为变化。⚠️ 因此每条都**先自证第一行真的落库**，再撞库 —— 否则
   "根本没写进去"的实现也会让 `IntegrityError` 看着像约束生效。每条另配**对照**
   （换一个应用 / 换一个 `scope` ⇒ **允许**），否则"什么都拒"也能让撞库那条绿。

⚠️ 只覆盖**数据面**：认证 / 令牌 / 幂等 / 限流 / 端点 / **投递逻辑**分属其它任务，
⛔ 本文件不测那些逻辑（投递引擎的判据在 `tests/test_openapi_dispatch.py`）。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C），⛔ 与任何真实租户 / 真实业务数据无关。
⚠️ 断言打在 `_meta` / **数据库行**这些真实出口上，⛔ 不打 logger、不看 `response.data`（`E-0063`）；
字段名与状态值写**字面量**，⛔ 不从常量模块 import（`E-0024`：钉值不钉常量）。
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, models as django_models, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.takerapi.models import (
    TakerApiDelivery,
    TakerApiIdempotency,
    TakerApiRateBucket,
    TakerApp,
)


class TakerAppModelTests(TenantTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="takerapi-owner", password="pw-123456"
        )
        self.admin = get_user_model().objects.create_user(
            username="takerapi-admin", password="pw-123456"
        )

    def _app(self, name="测试打手应用甲", app_key="takerkey000000a", **kwargs):
        return TakerApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret=kwargs.pop("app_secret", "testsecret00000000a"),
            taker=kwargs.pop("taker", self.user),
            created_by=kwargs.pop("created_by", self.admin),
            **kwargs,
        )

    # ── 1. 列**恰好**是第一期的 10 列 ＋ 第二期回调 2 列（+ Django 自己的 `id`）──
    def test_the_columns_are_exactly_the_spec_ones(self):
        """⛔⭐ `TakerApp` 的列**恰好**等于下面这一组（第一期 spec §3.1 的 10 列 ＋ 第二期 §3.1 的 2 列 + `id`）。

        ## 为什么是"恰好集合"而不是"不包含那几个名字"

        第一期 spec §3.1 末段点名⛔ **不放**权限节点 M2M / 游戏 M2M（范围**从 `taker` 那一行算**）。
        用"不包含"写的话，换个名字（`scope_nodes`）加回来照样绿；"恰好集合"会逼人当场说清
        每一列是什么、谁写它、留痕在哪。

        ## 为什么取 `local_fields` **加上** `local_many_to_many`

        `concrete_fields` 按 Django 自己的定义**不含 M2M**（它们没有列、住在中间表里）
        ⇒ 只写 `concrete_fields` 时，把游戏 M2M 加回来这条**照样绿**（先例实测踩到过，
        见 `tests/test_openapi_model.py` 那段）。`local_fields | local_many_to_many`
        才是"本表自己声明的东西"的恰好集合（不含反向关联与被继承的字段）。

        ⚠️ `id` 在集合里：它是 Django 自动主键、不是 spec 的那些列之一
        ⇒ 下面的字面量把它单列一行，读的人一眼看得出"12 列 + 1 个自增主键"。

        ⚠️⭐ **第二期 T2 把 `callback_url` / `callback_state` 从"不许有"翻成"必须在"**
        （第二期 spec §3.1，`docs/specs/2026-10-09-taker-callback-design.md`）：
        旧句的理由是"第一版没有主动回调推送 ⇒ 收了不生效是**静默**"；第二期把**入队 / 投递 /
        台账 / 页面**一并接上（T3–T6），那个前提不再成立。
        ⛔ **两条列清单都要逐字更新**（本条 + `test_the_forbidden_columns_do_not_exist`）。

        ⚠️ **弄红它**：给 `TakerApp` 加一列（例如 `notify_secret`），或在这张清单里
        删掉一列不删代码 —— 两种都会让这条红。
        """
        declared = set(TakerApp._meta.local_fields) | set(TakerApp._meta.local_many_to_many)
        self.assertEqual(
            sorted(f.name for f in declared),
            [
                # ⚠️ 字面量按**字典序**排（判据是 `sorted(...)`）：`id` 因此排在
                # `created_by` 与 `ip_allowlist` 之间，⛔ 不要为了"把 12 列聚在一起"
                # 把它挪到末尾 —— 那会让这条以"排序对不上"的形式假红。
                "app_key", "app_secret",
                # ⭐ 第二期那两个回调列（字典序里 `callback_state` 在 `callback_url` 之前）。
                "callback_state", "callback_url",
                "created_at", "created_by",
                "id",
                "ip_allowlist", "last_used_at", "name", "secret_rotated_at",
                "state", "taker",
            ],
        )

    # ── 2. 本 app 的模型名**恰好**四个 ──────────────────────────────
    def test_the_models_in_this_app_are_exactly_these(self):
        """本 app 的模型名**恰好**是这四个（第一期三张 ＋ 第二期 `TakerApiDelivery`）。

        判据打在 app registry 上（⛔ 不是"我没写"）：谁往这个 app 里加第五张表，
        这条当场红，逼他回 spec 补一行（⛔ 不许为了让网绿而放宽成 `assertIn`）。

        ⚠️⭐ **改准（T2）**：本条上一版写的是"恰好**三个**"，还把
        `TakerApiDelivery` 当**反面**例子（"spec §10-1 明确第一版不做推送，所以它不该存在"）。
        第二期（主动回调推送，`docs/specs/2026-10-09-taker-callback-design.md` §3.2）
        正是来建这张表的 ⇒ 旧句的前提失效，清单**逐字**补上它。⛔ 别照旧句理解成"它不该存在"。

        ⚠️ **弄红它**：把 `TakerApiDelivery` 从 `models.py` 删掉（这条当场红、
        `test_the_delivery_columns_are_exactly_the_spec_ones` 也会一起红），
        或反过来往这个 app 里加第五张表。
        """
        from django.apps import apps as django_apps

        model_names = sorted(
            m.__name__ for m in django_apps.get_app_config("takerapi").get_models()
        )
        self.assertEqual(
            model_names,
            [
                "TakerApiDelivery",
                "TakerApiIdempotency",
                "TakerApiRateBucket",
                "TakerApp",
            ],
        )

    # ── 3. ⛔ 不许有的列（逐条 `FieldDoesNotExist`）──────────────────
    def test_the_forbidden_columns_do_not_exist(self):
        """⛔ 点名不许有的东西**不是"现在没用"，是字段不存在**。

        判据打在 `_meta.get_field` 上（⛔ 不是 `hasattr`）：后者对"字段已删、但类上还留了个
        property / 类属性"会返回真，而那正是本用例要挡的形状。

        - `tenant_id` / `tenant`：本 app 在 `TENANT_APPS` 里 ⇒ 每个租户 schema 各一份，
          租户上下文只来自认证态 / schema 本身（`AGENTS.md` §4 的红线）。

        ⚠️⭐ **改准（T2）**：本条上一版把 `callback_url` / `callback_state`
        也列在"不许有"里（第一期：没有推送 ⇒ 连列都不放）。第二期**加了这两列**
        （`docs/specs/2026-10-09-taker-callback-design.md` §3.1）⇒ 它们从本条**移出**，
        改由 `test_the_columns_are_exactly_the_spec_ones` 逐字钉住"必须有"。
        ⚠️ 判别力**没有净损失**：移出的是"曾经不许有、现在必须有"的两列，
        而"恰好集合"那条本来就同时管"多了什么"。
        ⛔ 剩下的两样（租户列）**一条都没放宽**。

        ## ⭐ 第二期新表也守同一条

        `TakerApiDelivery` 同样**不许**有租户列（它是本 app 第四张表，口径与另三张一致）——
        ⛔ 别让它成为"新表漏守"的那一个。

        ⚠️ **弄红它**：加一个 `tenant_id = models.PositiveIntegerField(null=True)` ⇒ 本条红。
        """
        for gone in ("tenant_id", "tenant"):
            with self.subTest(model="TakerApp", field=gone):
                with self.assertRaises(FieldDoesNotExist):
                    TakerApp._meta.get_field(gone)

        # 四张表都不带租户列（同 app 的口径；⛔ 不是只有 `TakerApp` 一个人守这条）
        for model in (
            TakerApiDelivery,
            TakerApiIdempotency,
            TakerApiRateBucket,
        ):
            for gone in ("tenant_id", "tenant"):
                with self.subTest(model=model.__name__, field=gone):
                    with self.assertRaises(FieldDoesNotExist):
                        model._meta.get_field(gone)

    # ── 4. `taker` 必填 + PROTECT ──────────────────────────────────
    def test_taker_is_required_and_protected(self):
        """⭐ `taker` 是**必填**（`null=False` / `blank=False`）且 `on_delete is PROTECT`（spec §3.1）。

        两半各自的意义：
        - **必填**：范围判据的唯一来源不能为空 —— 一个没有绑定账号的应用"能看什么"无法回答，
          而 `null=True` 会让这种行**静默存在**（模板 / 序列化器上表现为一个空白格子）；
        - **`PROTECT`**：不许删掉一个还有接入应用的账号（删了之后应用行还在、指向的账号没了
          ⇒ 范围退化成"查不到那一行"，行为不可推理）。⚠️ 这是**字段级**判据，
          ⛔ 与"应用只能停用不能删"那条无关（后者保护的是应用自己）。

        ⚠️ **弄红它**：把 `on_delete` 改成 `CASCADE`（下面 `ProtectedError` 那条红），
        或把字段改成 `null=True, blank=True`（前两条断言红）。⚠️ 约束类变异要**模型与迁移两处
        一起改**，否则 Django 按迁移建表、变异没生效却报 SURVIVED。
        """
        field = TakerApp._meta.get_field("taker")
        self.assertFalse(field.null, "taker 必须必填（null=False）")
        self.assertFalse(field.blank, "taker 必须必填（blank=False）")
        self.assertIs(field.remote_field.on_delete, django_models.PROTECT)
        self.assertEqual(field.remote_field.model, get_user_model())

    def test_taker_protects_the_bound_user_from_deletion(self):
        """⭐ 删「有接入应用的打手账号」必须被数据库挡住（`PROTECT` 的**行为**判据）。

        ⚠️ 只断言 `on_delete is PROTECT` 是**声明**级判据；这一条是**行为**级判据
        （数据库真的会拒），两条一起才钉住（同族：`tests/test_openapi_model.py` 的
        `test_created_by_protects_the_user_from_deletion`）。

        ⚠️ **弄红它**：把 `on_delete` 改成 `CASCADE` 并重新生成迁移 ⇒ 下面 `ProtectedError`
        那条红（用户被删掉、应用行也没了）。
        """
        app = self._app(name="有绑定的应用", app_key="takerprotect0001")
        with self.assertRaises(ProtectedError):
            self.user.delete()
        self.assertTrue(TakerApp.objects.filter(pk=app.pk).exists())
        self.assertTrue(
            get_user_model().objects.filter(username="takerapi-owner").exists()
        )

    def test_user_without_apps_can_still_be_deleted(self):
        """对照：没建过接入应用的账号**照常可删** —— 否则上一条可能是"谁都删不掉"造成的假绿。"""
        other = get_user_model().objects.create_user(
            username="takerapi-no-app", password="pw-123456"
        )
        other.delete()
        self.assertFalse(
            get_user_model().objects.filter(username="takerapi-no-app").exists()
        )

    def test_the_two_user_links_are_taker_and_created_by(self):
        """指向账号的关联**恰好**两条：`taker`（范围）与 `created_by`（留痕）。

        ⭐ 这条挡的是"授权面悄悄长回来"：范围**只能**从 `taker` 那一行算（spec §3.1 末段：
        ⛔ 不建第二套授权存储）。若哪天有人加一个 `scope_owner` / `service_account` 之类的
        第二个账号指向，`taker` 就不唯一是范围来源了 —— 而那种改动**在端点用例里看不出来**
        （新列可以只躺在库里、不接在任何查询上，于是所有端点用例照样绿）。

        ⚠️ **弄红它**：加一个 `owner = models.ForeignKey(AUTH_USER_MODEL, ...)` ⇒ 这条红。
        """
        user_links = sorted(
            f.name
            for f in TakerApp._meta.get_fields()
            if getattr(f, "related_model", None) is get_user_model()
        )
        self.assertEqual(user_links, ["created_by", "taker"])

    # ── 5. `app_secret` 存库**可读**（正面判据）──────────────────────
    def test_app_secret_is_stored_readable(self):
        """`app_secret` 明文入库、读回来**逐字相同**（spec §3.1：验签要用它算 HMAC）。

        ⚠️ 这是**正面**判据、⛔ 不是"不许出现该字段"：有人把这一列改成哈希 / `write_only`
        之类的"安全加固"时，验签**当场不工作**，而这条会红（否则它会以"看起来更安全"的形态
        悄悄破坏既有口径）。
        ⚠️ 本条**不**管"密钥进不进日志 / 留痕"（那是 T3 的判据，spec §9-11），
        ⛔ 别把两件事合成一条。
        """
        app = self._app(
            name="可读密钥应用", app_key="takersecret00001", app_secret="synthetic-secret-0001"
        )
        self.assertEqual(
            TakerApp.objects.get(pk=app.pk).app_secret, "synthetic-secret-0001"
        )

    def test_state_defaults_to_enabled_in_the_database_row(self):
        """`state` 的库里默认值是 `enabled`（spec §3.1：唯一开关；停用 = 秒级失效）。

        ⚠️ 判据打在**库里的那一行**上：`objects.create()` 不吃类属性默认值
        ⇒ "类上写着 default 但库里是别的值 / 空值"这种形状只有读回来才抓得到。

        ⚠️ **弄红它**：把 `default=AppState.ENABLED` 换成 `DISABLED`（模型 + 迁移一起改）。
        """
        app = self._app(name="默认状态应用", app_key="takerstate000001")
        self.assertEqual(TakerApp.objects.get(pk=app.pk).state, "enabled")   # 钉字面值（E-0024）

    def test_ip_allowlist_defaults_to_empty_string(self):
        """`ip_allowlist` 的库里默认值是**空串**（spec §3.1：**空 = 不限制**）。

        ⚠️ 判别力：默认值若被写成 `0.0.0.0/0` 之类"看着更明确"的值，语义就从"不限制"
        变成"一条需要被解析的规则"（多一个解析失败的面）；而只断言类属性时看不出来。
        """
        app = self._app(name="白名单默认应用", app_key="takeriplist00001")
        row = TakerApp.objects.get(pk=app.pk)
        self.assertEqual(row.ip_allowlist, "")
        self.assertIsNone(row.last_used_at)
        self.assertIsNone(row.secret_rotated_at)
        self.assertIsNotNone(row.created_at)

    # ── ⭐ 第二期：回调那两列的形状（spec §3.1）──────────────────────
    def test_callback_url_width_is_200_and_blank(self):
        """⭐ `callback_url` = `URLField(max_length=200, blank=True, default="")`（第二期 spec §3.1）。

        ⚠️ `max_length == 200` 是**承重**的取值（理由写在模型的列注释里）：它是
        「**页面输入框的限长与库列宽同源**」这条的载体 —— 列宽被改小而不改页面限长，
        就会退化成"页面让填、库里**静默截断**"（保存看着成功、投递打到一个残地址上）。

        ⚠️ 断言打在**列的属性**上（`_meta.get_field`），⛔ 不是读类上的字面量：
        后者在"字段换了实现"时照样绿。
        ⚠️ 同时也钉 `blank=True`（地址为空是**合法状态**：三条判据里"地址为空"那一档
        就是靠它表达的 —— 见 `TakerApiDelivery` 的类 docstring 与第二期 spec §5）。
        ⛔ `null=True` **不许**加：URL 列在 Django 的口径里"空 = 空串"，两个空值并存会让
        「地址为空」这条判据出现两种表示（`""` 与 `NULL`）。

        ⚠️ **弄红它**：把 `max_length` 改成 100（模型 + 迁移一起改），或把 `blank` 去掉。
        """
        field = TakerApp._meta.get_field("callback_url")
        self.assertIsInstance(field, django_models.URLField)
        self.assertEqual(field.max_length, 200)
        self.assertTrue(field.blank, "地址为空必须合法（三条判据缺一不可，见模型 docstring）")
        self.assertFalse(field.null, "URL 列不许 null：两种空值表示会让『地址为空』有两套判据")
        self.assertEqual(field.default, "")

    def test_callback_columns_default_in_the_database_row(self):
        """⭐ 新建应用的两列库里默认值：`callback_url` = 空串、`callback_state` = `enabled`（字面量）。

        ⚠️ 判据打在**库里的那一行**上（`objects.create()` 不吃类属性默认值）：
        "类上写着 default 但库里是别的值 / 空值"这种形状只有读回来才抓得到。
        ⚠️ `enabled` 写成**字面量**、⛔ 不从 `CallbackState` import（`E-0024`：钉值不钉常量）
        —— 拿常量自比的话，谁把枚举取值改成 `on`，两条会**一起**改、用例照样绿。

        ⚠️ 这里只钉"默认值是多少"，⛔ 不钉"默认 `enabled` 安不安全"（那是第二期 spec §3.1
        那段理由 + T3/T5 的收件人判据的事）。
        ⚠️ **弄红它**：把 `default=CallbackState.ENABLED` 换成 `DISABLED`
        （⚠️ 约束类变异要**模型与迁移两处一起改**，否则 Django 按迁移建表、变异没生效却报 SURVIVED）。
        """
        app = self._app(name="回调默认应用", app_key="takercbdef00001")
        row = TakerApp.objects.get(pk=app.pk)
        self.assertEqual(row.callback_url, "")
        self.assertEqual(row.callback_state, "enabled")   # 钉字面值（E-0024）

    def test_callback_state_choices_are_the_two_literal_values(self):
        """⭐ `callback_state` 的取值**恰好** `enabled` / `disabled`（第二期 spec §3.1）。

        ⚠️ 这两值与上家侧**逐字相同**（引擎按字符串比较，第二期 spec §2.2 第 4 项）——
        但枚举各归各家：打手侧那一份定义在 `apps/takerapi/models.py`，
        ⛔ **不从 `apps.openapi.models` import**（模型层不许跨 app 依赖）。
        ⛔ 所以这条判据只能按**字面值**写，⛔ 不能写成"等于上家侧那个枚举的 choices"
        （那就把"两族各有一份"这件事又合并成一份了）。

        ⚠️ **弄红它**：把 `CallbackState.ENABLED` 的取值改成 `"on"`（模型 + 迁移一起改）。
        """
        field = TakerApp._meta.get_field("callback_state")
        self.assertEqual(field.max_length, 16)
        self.assertEqual(
            [value for value, _label in field.choices],
            ["enabled", "disabled"],
        )
        self.assertEqual(field.default, "enabled")


class TakerApiUniqueConstraintTests(TenantTestCase):
    """⭐ 三条唯一约束的**数据库级**判据（spec §3.1 / §3.2 / §3.3）。

    ⚠️ 这里与 `TakerAppModelTests` 分开：那些是"列的形状"，这些是"库真的会拦"。
    每条撞库用例都**先自证第一行真的落库**（否则"根本没写进去"也会看着像约束生效），
    并配一条**对照**证明约束的**边界**（换应用 / 换 `scope` ⇒ 允许）——
    没有对照时，"什么都拒"的实现也能让撞库那条绿。
    """

    def setUp(self):
        self.taker = get_user_model().objects.create_user(
            username="takerapi-c-owner", password="pw-123456"
        )
        self.creator = get_user_model().objects.create_user(
            username="takerapi-c-admin", password="pw-123456"
        )

    def _app(self, name, app_key):
        return TakerApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret="testsecret00000000a",
            taker=self.taker,
            created_by=self.creator,
        )

    # ── `TakerApp.name` / `TakerApp.app_key` 的两条 `unique=True` ──────
    def test_duplicate_app_key_is_rejected(self):
        """`app_key` 的唯一索引**真的在数据库上**（spec §3.1：按 key 一次索引查一行）。

        ⚠️ 那句"唯一性是承重的"的承重点在这里：掉了它，验签会退化成逐行扫哈希
        （功能上看不出异常，只有量级变化；peer 那套就是这么做的，spec §3.1 明令**不照抄**）。
        """
        self._app(name="应用甲", app_key="takerdupkey00001")
        self.assertEqual(TakerApp.objects.filter(app_key="takerdupkey00001").count(), 1)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._app(name="应用乙", app_key="takerdupkey00001")

    def test_duplicate_name_is_rejected(self):
        """应用名在**租户内唯一**（人工指认用，spec §3.1）。"""
        self._app(name="重名打手应用", app_key="takernamekey0001")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._app(name="重名打手应用", app_key="takernamekey0002")

    def test_the_app_key_lookup_returns_exactly_one_row(self):
        """按 `app_key` **一次查询命中一行**（spec §3.1 的取数形态，⛔ 不是逐行扫哈希）。"""
        created = self._app(name="可查应用", app_key="takerlookup000001")
        self._app(name="另一个应用", app_key="takerlookup000002")
        hit = TakerApp.objects.filter(app_key="takerlookup000001").first()
        self.assertIsNotNone(hit)
        self.assertEqual(hit.pk, created.pk)

    # ── `TakerApiIdempotency` 的 `(app, key)` ─────────────────────────
    def test_idempotency_duplicate_key_for_same_app_is_rejected(self):
        """⭐ `(app, key)` 唯一约束（spec §3.2）；约束名 = `uniq_taker_idem_app_key`。"""
        app = self._app(name="幂等应用", app_key="takeridemkey00001")
        payload = dict(
            method="POST",
            path="/api/taker/v1/hall/TT-TAKER-0001/claim/",
            request_hash="0" * 64,
            response_snapshot={"code": 0, "data": {"order_no": "TT-TAKER-0001"}},
            status_code=200,
        )
        first = TakerApiIdempotency.objects.create(app=app, key="taker-idem-key-0001", **payload)
        self.assertEqual(
            TakerApiIdempotency.objects.filter(app=app, key="taker-idem-key-0001").count(), 1
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TakerApiIdempotency.objects.create(app=app, key="taker-idem-key-0001", **payload)
        # 撞库之后原来那一行还在（约束是"拒第二条"，⛔ 不是"清表"）
        self.assertEqual(TakerApiIdempotency.objects.get(pk=first.pk).status_code, 200)

    def test_idempotency_same_key_under_another_app_is_allowed(self):
        """⭐ **对照**：唯一性是 **`(app, key)`**、⛔ 不是 `key` 全局唯一（spec §3.2）。

        两个应用撞同一个键是**合法**的（不同的接入方各自用同一套键名）。
        判别力：若给 `key` 加 `unique=True`，这条会红；而上面那条撞库用例**照样绿**
        ⇒ 两条合起来才钉住"唯一性是按应用分的"。
        """
        app_a = self._app(name="幂等应用甲", app_key="takeridemapp000a")
        app_b = self._app(name="幂等应用乙", app_key="takeridemapp000b")
        common = dict(
            method="POST",
            path="/api/taker/v1/hall/TT-TAKER-0002/claim/",
            request_hash="1" * 64,
            response_snapshot={"code": 0, "data": None},
            status_code=200,
        )
        TakerApiIdempotency.objects.create(app=app_a, key="taker-shared-key-01", **common)
        TakerApiIdempotency.objects.create(app=app_b, key="taker-shared-key-01", **common)
        self.assertEqual(
            TakerApiIdempotency.objects.filter(key="taker-shared-key-01").count(), 2
        )

    # ── `TakerApiRateBucket` 的 `(scope, ident, window_start)` ─────────
    def test_rate_bucket_duplicate_scope_ident_window_is_rejected(self):
        """⭐ `(scope, ident, window_start)` 唯一约束（spec §3.3）；约束名 = `uniq_taker_rate_bucket`。

        ⚠️ **第二次插入必须复用同一个 `window_start` 值**（先取进变量再传两遍）：
        两边各调一次 `timezone.now()` 会拿到**微秒不同**的两个时刻 ⇒ 那是两行**合法**的窗口，
        约束不该拦，而用例会以 `IntegrityError not raised` **假红**（同族先例实测踩到过）。
        """
        window = self._window()
        bucket = TakerApiRateBucket.objects.create(
            scope="app", ident="taker-bucket-appkey-01", window_start=window, count=1
        )
        self.assertEqual(TakerApiRateBucket.objects.filter(pk=bucket.pk).count(), 1)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TakerApiRateBucket.objects.create(
                    scope="app",
                    ident="taker-bucket-appkey-01",
                    window_start=window,          # ⚠️ 同一个时刻（⛔ 不重新 now()）
                    count=1,
                )
        self.assertEqual(TakerApiRateBucket.objects.get(pk=bucket.pk).count, 1)

    def test_rate_bucket_same_three_values_under_ip_scope_is_allowed(self):
        """⭐ **对照**：只把 `scope` 换成 `ip`、其余三列**逐字相同** ⇒ **允许**插进去。

        这条证的是 **`scope` 真的进了唯一键**（⛔ 不是摆设）：少了它，同 `ident` / 同窗口的
        第二行会被上面那条约束挡住，而这条对照当场红。
        """
        window = self._window()
        ident_text = "taker-bucket-shared-01"   # 两行**故意**用同一个文本，才分辨得出 scope 的贡献
        TakerApiRateBucket.objects.create(scope="app", ident=ident_text, window_start=window, count=7)
        TakerApiRateBucket.objects.create(scope="ip", ident=ident_text, window_start=window, count=1)

        self.assertEqual(
            TakerApiRateBucket.objects.filter(ident=ident_text, window_start=window).count(), 2
        )
        self.assertEqual(TakerApiRateBucket.objects.get(scope="app", ident=ident_text).count, 7)
        self.assertEqual(TakerApiRateBucket.objects.get(scope="ip", ident=ident_text).count, 1)

    def test_rate_bucket_count_starts_at_zero(self):
        """`count` 初值 = 0（spec §3.3 的固定窗口：先建行、再 UPSERT 累加）。"""
        bucket = TakerApiRateBucket.objects.create(
            scope="ip", ident="203.0.113.7", window_start=self._window()
        )
        self.assertEqual(TakerApiRateBucket.objects.get(pk=bucket.pk).count, 0)

    @staticmethod
    def _window():
        """固定窗口的左端。⚠️ 值本身无语义，但**同一个用例里两次调用必须相等** ⇒ 先取好再传。"""
        return timezone.now()


class TakerApiConstraintNameTests(TenantTestCase):
    """⭐ 两条**具名**唯一约束的**名字与列**（spec §3.2 / §3.3 的对应关系）。

    ⚠️ 为什么单独钉名字：这两条约束是**具名**的（⛔ 不是 `unique_together` 或列上
    `unique=True`）——名字是迁移与库里的标识，改了名字等于换了约束，
    而 `IntegrityError` 那几条**照样绿**（约束还在，只是叫别的）⇒ 名字要有独立判据。
    ⚠️ 判据同时钉**列清单**：`"约束存在"与"约束在正确的列上"是两件事
    （名字对、列换了 ⇒ 约束形同虚设，而撞库用例可能因为换了列仍然会撞而假绿）。
    """

    def test_named_unique_constraints_and_their_columns(self):
        idem = {c.name: tuple(c.fields) for c in TakerApiIdempotency._meta.constraints}
        self.assertEqual(
            idem, {"uniq_taker_idem_app_key": ("app", "key")}
        )
        bucket = {c.name: tuple(c.fields) for c in TakerApiRateBucket._meta.constraints}
        self.assertEqual(
            bucket, {"uniq_taker_rate_bucket": ("scope", "ident", "window_start")}
        )

    def test_app_key_is_a_single_column_unique_index(self):
        """`TakerApp.app_key` 的唯一性是**列级** `unique=True`（spec §3.1）。

        ⚠️ 与上面那两条**具名约束**分开：这一条没有名字，它的判据只能是列属性 + 库行为
        （库行为那条在 `TakerApiUniqueConstraintTests::test_duplicate_app_key_is_rejected`）。
        ⚠️ 同时钉 `name` 的 `unique=True`（spec §3.1 首列）——⛔ 别把它当成"顺手加的"：
        spec 明写 `unique=True`，而漏了它今天的端点用例**一条都不会红**。
        """
        self.assertTrue(TakerApp._meta.get_field("app_key").unique)
        self.assertTrue(TakerApp._meta.get_field("name").unique)
        self.assertEqual(TakerApp._meta.get_field("app_key").max_length, 64)
        self.assertEqual(TakerApp._meta.get_field("name").max_length, 50)
        self.assertEqual(TakerApp._meta.get_field("app_secret").max_length, 64)
        self.assertEqual(TakerApp._meta.get_field("state").max_length, 16)

    def test_the_two_single_column_indexes_exist(self):
        """两条**单列**索引（幂等台账的 `created_at` / 限流桶的 `window_start`，spec §3.2 / §3.3）。

        ⚠️ 它们**不是**"顺手加的"：各自供一条机会式清理的 `DELETE … WHERE <单列> < 时刻`，
        而那两条唯一索引的前缀分别是 `app` / `scope` ⇒ **替不了**它。掉了它们清理会退化成
        全表扫，而功能与其它用例**一条都不会红**（只有量级变化）。
        """
        def index_map(model):
            return {
                tuple(i.fields): i.name
                for i in model._meta.indexes
            }

        self.assertIn(("created_at",), index_map(TakerApiIdempotency))
        self.assertIn(("window_start",), index_map(TakerApiRateBucket))


class TakerApiDeliveryModelTests(TenantTestCase):
    """⭐ 第二期新表 `TakerApiDelivery`（打手侧的 outbox + 台账）的**模型级判据**（spec §3.2）。

    ⚠️ 这一份**只测列 / 枚举 / `Meta`**（"表长得对不对"）；投递行为（退避 / 租约 / 去重 /
    收件人）分属 T3/T4，判据在 `tests/test_openapi_dispatch.py` 与打手侧自己的网里。
    ⛔ 两条**不能混**：把"表形状"与"投递语义"合成一条，红的读数是"哪里错了"都说不清。
    """

    def setUp(self):
        self.taker = get_user_model().objects.create_user(
            username="takerapi-d-taker", password="pw-123456"
        )
        self.creator = get_user_model().objects.create_user(
            username="takerapi-d-admin", password="pw-123456"
        )

    def _app(self, name="台账夹具应用甲", app_key="takerdelivapp001"):
        return TakerApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret="testsecret00000000a",
            taker=self.taker,
            created_by=self.creator,
        )

    # ── 列**恰好**是 spec §3.2 那 12 列（+ `id`）────────────────────
    def test_the_delivery_columns_are_exactly_the_spec_ones(self):
        """⛔⭐ 列名**恰好**等于第二期 spec §3.2 那 12 列 + `id`（逐一对应 `OpenApiDelivery`）。

        ⚠️ 与 `TakerApp` 那条同款理由（`local_fields | local_many_to_many` 才是"本表自己声明的
        东西"的恰好集合）：用"不包含某几个名字"写的话，换名字加列照样绿。
        ⚠️ 12 列**逐一对应**上家侧那张表是 **spec §3.2 的原文要求**（引擎只有一份，
        它按 `spec.delivery_model` 现取这些列名 ⇒ 列名 / 列宽对不上就是**运行期**炸，
        而"运行期才炸"在模型层这条网里应该先被挡住）。
        ⛔ **`Meta.constraints` 必须为空**（spec §3.2 末句：照上家侧那张，无约束）——
        有人加一条 `(app, event, order_no)` 唯一键就会**丢事件**（同一订单的状态可以变多次）。

        ⚠️ **弄红它**：加一列（例如 `tenant_id`），或删掉 `processed_at`。

        ⚠️ **弄红方式（约束那一半）**：给 `Meta` 加一条 `UniqueConstraint(fields=["app","event","order_no"])`
        —— 模型 + 迁移一起改。
        """
        declared = set(TakerApiDelivery._meta.local_fields) | set(
            TakerApiDelivery._meta.local_many_to_many
        )
        self.assertEqual(
            sorted(f.name for f in declared),
            [
                "app", "attempts",
                "created_at",
                "event",
                "id",
                "next_attempt_at", "order_no",
                "payload", "processed_at",
                "reason", "response_status",
                "source_order_no", "status",
            ],
        )
        self.assertEqual(list(TakerApiDelivery._meta.constraints), [])

    # ── ⭐ `Event` 取值**恰好**四个（含本线独有的 `order.reassigned`）──
    def test_the_event_values_are_exactly_these(self):
        """⭐ `Event` 的取值**恰好**四个：三类与上家侧**逐字相同** ＋ 本线独有的 `order.reassigned`。

        ⚠️ 前三个值与上家侧那三个**逐字相同**（引擎只按字符串比较，spec §2.2 第 4 项），
        但这里写**字面量**、⛔ 不 import 上家侧的枚举（`E-0024` 钉值不钉常量；
        ⛔ 也⛔ 不从 `apps.openapi.models` 借那一份 —— 模型层不许跨 app 依赖）。
        ⚠️ 第四条是**本线唯一新增**的语义（spec §1 末段 / §4.1）：「这单不再是你的了」——
        不推它的话，外部系统会在一个**已经被拿走的单**上继续干活。
        ⚠️ 标签（第二项）也一起钉：它是对外契约里那句中文字面值（spec §3.2 / §4.1）。

        ⚠️ **弄红它**：删掉 `ORDER_REASSIGNED`（本用例红），或把它的值改成
        `"order.reassign"`（本用例红，而**别的**用例多半照样绿）。
        """
        self.assertEqual(
            [(value, label) for value, label in TakerApiDelivery.Event.choices],
            [
                ("order.status_changed", "订单状态变化"),
                ("order.deleted", "订单删除"),
                ("order.image_added", "订单新增进度图"),
                ("order.reassigned", "订单不再属于该接手人"),
            ],
        )

    def test_the_status_values_are_exactly_these(self):
        """⭐ `Status` 的取值**恰好** `pending` / `done` / `failed`（逐字同于上家侧，spec §3.2）。

        ⚠️ 为什么钉字面值：**引擎按 `spec` 里的字符串比较**（`spec.delivery_pending_value` 等）
        ⇒ 谁把这几个值改一个字母，引擎的比较会**静默**永远不命中（行永远停在 `pending`
        或永远重复投递），而功能上"看着还在跑"。这条网是那个形状的**唯一**早期信号。
        ⚠️ 默认值 `pending` 一并钉（写在 `field.default` 上）。

        ⚠️ **弄红它**：把 `PENDING` 的值改成 `"waiting"`（模型 + 迁移一起改）。
        """
        field = TakerApiDelivery._meta.get_field("status")
        self.assertEqual(
            [value for value, _label in field.choices],
            ["pending", "done", "failed"],
        )
        self.assertEqual(field.max_length, 16)
        self.assertEqual(field.default, "pending")

    def test_the_meta_ordering_and_the_three_indexes(self):
        """⭐ `Meta`：`ordering = ["-created_at", "-id"]` ＋ 三条索引（spec §3.2）。

        ⚠️ 三条索引**各有各的取数面**、⛔ 没有一条是"顺手加的"（理由逐条写在模型的 `Meta` 里）：
        ① `(status, next_attempt_at)` = 常驻投递进程**唯一**的取活条件；② `app` = 管理页按应用取
        最近 N 条；③ `created_at` = 机会式清理那条 `DELETE`（本表只增不减）。
        掉了任何一条，功能与其它用例**都不会红**（只有量级变化）⇒ 只能靠本条钉住。

        ⚠️ 索引**名字**也钉：名字是迁移与库里的标识，改了名字等于换了索引，
        而"索引还在"这件事照样成立（判据必须同时钉**名字**与**列**，照
        `TakerApiConstraintNameTests` 的同款理由）。

        ⚠️ 弄红它：删掉 `taker_deliv_created_idx`，或把 `ordering` 改成 `["-created_at"]`。
        """
        self.assertEqual(TakerApiDelivery._meta.ordering, ["-created_at", "-id"])
        index_map = {tuple(i.fields): i.name for i in TakerApiDelivery._meta.indexes}
        self.assertEqual(
            index_map,
            {
                ("status", "next_attempt_at"): "taker_deliv_due_idx",
                ("app",): "taker_deliv_app_idx",
                ("created_at",): "taker_deliv_created_idx",
            },
        )

    # ── 外键的形状：指向**打手侧**应用、CASCADE、`related_name="deliveries"` ──
    def test_the_app_link_points_at_the_taker_side_app(self):
        """⭐ `app` 指向 `takerapi.TakerApp`（⛔ **不是**上家侧那张应用表）、`CASCADE`、
        `related_name="deliveries"`（spec §3.2）。

        ⚠️ 这条挡的是**最贵的那个形状**（第二期 spec §2.2 第 10 项）：两族的投递台账
        指到**同一张**应用表 / 或干脆共表 ⇒ 引擎照常跑、一行异常都没有，只有
        "一族的回调投递给错的人"这一件事**没有任何症状**。
        ⚠️ 判据是 `remote_field.model` 的**身份**（⛔ 不是模型名字符串）：字符串相等
        在"两族各有同名模型"时会假绿。

        ⚠️ **弄红它**：把 `to=` 改成指向 `apps.openapi.models.ApiApp`（+ 迁移），
        或把 `related_name` 改掉（反向访问器 `app.deliveries` 会消失 —— 台账页按它取数）。
        """
        field = TakerApiDelivery._meta.get_field("app")
        self.assertIs(field.remote_field.model, TakerApp)
        self.assertIs(field.remote_field.on_delete, django_models.CASCADE)
        self.assertEqual(field.remote_field.related_name, "deliveries")
        self.assertEqual(field.target_field.name, "id")

    def test_the_reverse_accessor_returns_the_rows_of_that_app_only(self):
        """⭐ **行为**级判据：`app.deliveries` 只给**这个应用**的行（spec §3.2 的 `related_name`）。

        ⚠️ 只断言 `related_name` 是**声明**级判据；这条证明它真的按 `app_id` 过滤
        （写死了别的应用就退化成"所有行" —— 台账页会串台）。
        ⚠️ 夹具用**合成值**，且两个应用**同租户**（跨租户那条由 `test_tenant_isolation` 管）。
        """
        app_a = self._app(name="台账应用甲", app_key="takerdelirow000a")
        app_b = self._app(name="台账应用乙", app_key="takerdelirow000b")
        TakerApiDelivery.objects.create(
            app=app_a, event="order.status_changed", order_no="TT-TAKER-D-0001"
        )
        TakerApiDelivery.objects.create(
            app=app_a, event="order.deleted", order_no="TT-TAKER-D-0002"
        )
        TakerApiDelivery.objects.create(
            app=app_b, event="order.deleted", order_no="TT-TAKER-D-0003"
        )
        self.assertEqual(
            sorted(app_a.deliveries.values_list("order_no", flat=True)),
            ["TT-TAKER-D-0001", "TT-TAKER-D-0002"],
        )
        self.assertEqual(
            list(app_b.deliveries.values_list("order_no", flat=True)),
            ["TT-TAKER-D-0003"],
        )

    def test_the_defaults_and_the_nullable_columns_in_the_database_row(self):
        """⭐ 库里那一行的默认值：`attempts`=0 / `reason`="" / `source_order_no`="" /
        `payload`={} / `next_attempt_at` / `response_status` / `processed_at` 三个**为空**
        （spec §3.2 逐列）。

        ⚠️ 判据打在**库里的那一行**上（`objects.create()` 不吃类属性默认值）。
        ⚠️ 三个"为空"的列**都有语义**：`next_attempt_at` 为空 = **立刻可投**、
        `processed_at` 为空 = **还没落终态**、`response_status` 为空 = **还没收到过回执**
        ⇒ 把它们写成 `0` / `now()` 之类的"更明确"的值会让"待发"与"已终态"两种行**分不开**
        （而取活查询正是按前两列筛的）。

        ⚠️ **弄红它**：把 `attempts` 的默认值改成 1，或把 `next_attempt_at` 改成 `default=timezone.now`。
        """
        app = self._app()
        row = TakerApiDelivery.objects.create(
            app=app, event="order.status_changed", order_no="TT-TAKER-D-0004"
        )
        stored = TakerApiDelivery.objects.get(pk=row.pk)
        self.assertEqual(stored.status, "pending")
        self.assertEqual(stored.attempts, 0)
        self.assertEqual(stored.reason, "")
        self.assertEqual(stored.source_order_no, "")
        self.assertEqual(stored.payload, {})
        self.assertIsNone(stored.next_attempt_at)
        self.assertIsNone(stored.response_status)
        self.assertIsNone(stored.processed_at)
        self.assertIsNotNone(stored.created_at)

    def test_the_event_and_status_columns_are_32_and_16_wide(self):
        """列宽：`event` = 32、`order_no` / `source_order_no` = 64（spec §3.2 逐列）。

        ⚠️ 与上家侧**逐列同值**是要求（引擎只有一份）⇒ 这里钉的是"打手侧那张表照着那份抄对了"。
        ⚠️ `event` 的 32 是**唯一**放得下 `order.status_changed`（20 字符）还有余量的取值，
        而 `order_no` 64 是既有的订单号口径（与 `OpenApiDelivery` 同列宽）。

        ⚠️ **弄红它**：把 `event` 的 `max_length` 改成 16（模型 + 迁移一起改）。
        """
        self.assertEqual(TakerApiDelivery._meta.get_field("event").max_length, 32)
        self.assertEqual(TakerApiDelivery._meta.get_field("order_no").max_length, 64)
        self.assertEqual(
            TakerApiDelivery._meta.get_field("source_order_no").max_length, 64
        )
        self.assertEqual(
            TakerApiDelivery._meta.get_field("attempts").get_internal_type(),
            "PositiveSmallIntegerField",
        )
        self.assertEqual(
            TakerApiDelivery._meta.get_field("reason").get_internal_type(), "TextField"
        )
