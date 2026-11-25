"""个人绑定 + 本线链路两张新表的形状与唯一约束（下游鸡蛋铺线 Task 1）。

本模块只钉**表本身的形状**（字段默认值、唯一约束、跨行不共享）：
- `JidanpuMemberBinding`：一个代练员一份（同一行 ⇒ 覆盖；第二次 `create` 撞唯一约束）；
  同一套鸡蛋铺里同一个账号不重复绑（`(jidanpu_base_url, account)` 唯一）。
- `JidanpuMemberOrderLink`：`(binding, order)` 唯一 + `(binding, jidanpu_order_id)` 唯一。

⚠️ 判据打**数据库行**（`AGENTS.md` §5-G）：靠 `IntegrityError` 与字段值断言，⛔ 不打 logger。
⚠️ 每条唯一约束都配一条**反向用例**（换个维度就该放行）——只钉「重复会炸」证明不了
约束是**复合**的：把 `(url, account)` 误写成「`account` 单列唯一」、把 `(binding, order)`
误写成「`order` 全局唯一」，正向用例照样全绿。
⚠️ 默认值断言写**字面量** `3` / `180`（spec §4.1 的值），⛔ 不写 `== MEMBER_FAST_POLL_SECONDS`
——那是自指断言：常量被改坏时两边一起变、**永远不会红**（E-0024）。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `acc-a`、
密码 `pw-a`。⛔ 零真实网络——本模块只碰 ORM。

⚠️ 本模块另有一节：**官方 Open Platform API 那一组列 + 部分唯一索引 + 迁移重放**
（spec `docs/specs/2026-10-08-jidanpu-official-api-binding-design.md` §2.1 / §4.2 / §4.4）。
"""
import re

from django.contrib.auth.models import User
from django.db import IntegrityError, connection, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu_member, jidanpu_openapi
from apps.business.models import Game, JidanpuMemberBinding, JidanpuMemberOrderLink, Order

BASE_URL_A = "https://jidanpu-a.test.invalid"
BASE_URL_B = "https://jidanpu-b.test.invalid"

#: 两串**合成**码（22 位，形状 = `[A-Za-z0-9_-]{22}`）：造「存量：同一个域名两串码」那个形态用。
LEGACY_CODE_A = "Zx9_legacy_A_member022"
LEGACY_CODE_B = "Zx9_legacy_B_member022"
assert len(LEGACY_CODE_A) == len(LEGACY_CODE_B) == 22


def _make_binding(*, username="member-a", base_url=BASE_URL_A, account="acc-a"):
    user = User.objects.create_user(username=username, password="pw-a")
    return JidanpuMemberBinding.objects.create(
        user=user, jidanpu_base_url=base_url, account=account, password="pw-1",
    )


class MemberBindingShapeTests(TenantTestCase):
    def test_one_row_per_user(self):
        """一个代练员同时只有一份绑定（覆盖同一行 ⇒ 第二次 create 必须撞唯一约束）。"""
        user = User.objects.create_user(username="member-a", password="pw-a")
        JidanpuMemberBinding.objects.create(
            user=user, jidanpu_base_url="https://jidanpu-a.test.invalid", account="acc-a", password="pw-1",
        )
        with self.assertRaises(IntegrityError):
            JidanpuMemberBinding.objects.create(
                user=user, jidanpu_base_url="https://jidanpu-a.test.invalid", account="acc-b", password="pw-2",
            )

    def test_base_url_and_account_unique(self):
        """同一套鸡蛋铺的同一个账号不重复绑（不同代练员也不行）。"""
        _make_binding(username="member-a")
        other = User.objects.create_user(username="member-b", password="pw-b")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                JidanpuMemberBinding.objects.create(
                    user=other, jidanpu_base_url=BASE_URL_A, account="acc-a", password="pw-2",
                )

    def test_same_account_on_another_base_url_is_allowed(self):
        """账号唯一是**跟着地址走的**：另一个地址上的同名账号必须放行。"""
        _make_binding(username="member-a", base_url=BASE_URL_A, account="acc-a")
        other = User.objects.create_user(username="member-b", password="pw-b")
        binding = JidanpuMemberBinding.objects.create(
            user=other, jidanpu_base_url=BASE_URL_B, account="acc-a", password="pw-2",
        )
        self.assertEqual(binding.account, "acc-a")

    def test_defaults_match_the_spec(self):
        """`fast_poll_seconds` / `reconcile_seconds` 落库的默认值就是 spec §4.1 的 3 / 180。"""
        binding = _make_binding()
        self.assertEqual(binding.fast_poll_seconds, 3)
        self.assertEqual(binding.reconcile_seconds, 180)
        self.assertFalse(binding.enabled)
        self.assertIsNone(binding.checked_at)


class MemberOrderLinkShapeTests(TenantTestCase):
    def setUp(self):
        self.binding = _make_binding(username="member-a")
        game = Game.objects.create(name="合成游戏甲")
        self.order_a = Order.objects.create(game=game, order_no="20260926000000001901")
        self.order_b = Order.objects.create(game=game, order_no="20260926000000001902")

    def test_one_link_per_binding_and_order(self):
        """同一绑定里一张我方订单只有一条链路。"""
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.order_a, jidanpu_order_id="remote-1",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                JidanpuMemberOrderLink.objects.create(
                    binding=self.binding, order=self.order_a, jidanpu_order_id="remote-2",
                )

    def test_remote_order_id_unique_within_binding(self):
        """同一绑定里一个远端单号只能挂一条链路。"""
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.order_a, jidanpu_order_id="remote-1",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                JidanpuMemberOrderLink.objects.create(
                    binding=self.binding, order=self.order_b, jidanpu_order_id="remote-1",
                )

    def test_same_order_and_remote_id_under_another_binding_is_allowed(self):
        """两条唯一约束都是**按绑定**算的：换了绑定，同单 + 同远端单号必须放行。

        （spec §4.2 选甲的代价：一张单可以同时挂在租户级那条线与本线里，两套绑定互不感知。）
        """
        other_binding = _make_binding(
            username="member-b", base_url=BASE_URL_B, account="acc-b"
        )
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.order_a, jidanpu_order_id="remote-1",
        )
        link = JidanpuMemberOrderLink.objects.create(
            binding=other_binding, order=self.order_a, jidanpu_order_id="remote-1",
        )
        self.assertFalse(link.in_hall)


# ── 官方 Open Platform API 那一组列 + 部分唯一索引（spec §2.1 / §4.2 / §4.4）──────────
# ⚠️ 本节判据一律打在**数据库行 / 数据库里的索引定义 / 迁移文件里的默认值**上
#    （`AGENTS.md` §5-G）：「迁移后既有绑定仍是登录式」这条**不能**靠「读模型默认值」证明
#    —— 那只证明**新行**的默认值，说的是另一件事（既有行在迁移那一刻是被 `ADD COLUMN … DEFAULT`
#    回填的）。
# ⚠️ 本节**只加 8 列**（⛔ 不是 9）：`reconcile_seconds` 本表**已经有了**（默认 180、verbose
#    「对账间隔(秒)」）⇒ 复用它当②式兜底对账的闸门，⛔ 不许再加同名列
#    （上家线那一列虽然同名，但默认 600、verbose「兜底对账间隔(秒)」——语义不同）。

#: 那串随机码的形状（spec §5.1）：`secrets.token_urlsafe(16)`。
#: ⚠️ **22 是承重的**：`deploy/nginx/tuantuantong.conf` 的 location 用定长 22 的正则钉住回调路径。
CALLBACK_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22}$")

#: 下家线「地址 + 账号」那条唯一约束的名字（⛔ 不从模型派生）。
ACCOUNT_CONSTRAINT = "uniq_jdp_member_account"

#: ⭐ **旧**那条「一行一个码」的部分唯一索引的名字（按业务方新口径**已删除**）。
#: 它今天只在一个地方用得上：`test_the_old_code_index_is_gone_from_the_database`
#: ——留着旧索引的话，同一个域名下的第二条②式绑定**照样建不上**（只是把拒绝从「账号」挪到「码」）。
CALLBACK_CODE_CONSTRAINT = "uniq_jdp_member_callback_code"

#: 本表的名字（⛔ 独立的期望值，不是从被测模型派生出来的）。
MEMBER_BINDING_TABLE = "business_jidanpumemberbinding"

#: 本任务加的**恰好 8 列**（spec §4.2 的表减去 `reconcile_seconds`）。
#: ⭐ 它是**独立字面量**：从这个集合里少一个 = 漏加一列；多一个 = 加了本任务明禁的那一列。
NEW_COLUMNS = {
    "mode",
    "client_id",
    "client_secret",
    "callback_code",
    "callback_code_at",
    "callback_watermark_at",
    "callback_last_at",
    "callback_count",
}


def _make_openapi_binding(*, username="member-openapi", base_url=BASE_URL_B,
                          account="acc-openapi", code=None):
    """造一条**官方 API 方式**的下家绑定。

    ⚠️ `code` 显式给了就原样用（要构造「两行同码 / 空码」这类形态）；不给则由模型自己生成
    （⭐ 新口径下它会**复用同域名兄弟行**那一串，或者生成一串新的 —— 那正是被测的行为）。
    """
    user = User.objects.create_user(username=username, password="pw-a")
    return JidanpuMemberBinding.objects.create(
        user=user, jidanpu_base_url=base_url, account=account, password="pw-1",
        mode="openapi", client_id="test-client-id", client_secret="test-client-secret",
        **({} if code is None else {"callback_code": code}),
    )


def _index_definitions(table):
    """`pg_indexes` 里这张表每条索引的定义（`indexdef` 文本，含 `WHERE` 那一半）。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
            [connection.schema_name, table],
        )
        return {name: definition for name, definition in cursor.fetchall()}


def _adds_member_mode_column_operation(migration):
    """`migration` 里那条「给 `JidanpuMemberBinding` 加 `mode` 列」的 `AddField`（没有 ⇒ `None`）。

    ⭐ 语义标识的唯一出处：认的是 `AddField` 的 `model_name` + `name` 两个**语义**字段，
    ⛔ 不是文件名、⛔ 不是序号前缀、⛔ 不是它在图里的位置。
    ⚠️ `model_name` 必须是 **`jidanpumemberbinding`**（下家那张表）：写成
    `jidanpuupstreambinding` 会取到**上家线那条迁移**（`0057_*`），而本类的重放会退错结构
    ⇒ 这正是「序号前缀当标识」同族的坑（E-0213）。
    """
    for operation in migration.operations:
        if (type(operation).__name__ == "AddField"
                and operation.model_name == "jidanpumemberbinding"
                and operation.name == "mode"):
            return operation
    return None


def _migration_add_field_default(model_name, field_name):
    """迁移文件里那条 `AddField` 的 `default`（找不到那条操作就回 `None`）。

    ⚠️ 为什么读**迁移**而不是读模型：存量行是被 `ADD COLUMN … DEFAULT` 填的，
    「迁移写了什么默认值」与「模型 `field.default` 是什么」是**两份**可以各自漂移的东西
    （改了模型忘了同一次改迁移 ⇒ 新库建出来与旧库升上来的**语义不同**）。
    """
    loader = MigrationLoader(None, ignore_no_migrations=True)
    for (app_label, _name), migration in loader.disk_migrations.items():
        if app_label != "business":
            continue
        for operation in migration.operations:
            if (type(operation).__name__ == "AddField"
                    and operation.model_name == model_name
                    and operation.name == field_name):
                return operation.field.default
    return None


#: 造绑定行时写的列（原生 SQL）—— ⚠️ **刻意不含本任务那 8 列**：
#: 这份清单就是「加列之前那种行」的列集，给迁移重放用例在**结构已退回**时造夹具用。
#: ⚠️ 漏一个 `NOT NULL` 且**列上没有数据库默认值**的列 ⇒ 直接 `NotNullViolation`
#:    （Django 在 `ADD COLUMN … DEFAULT` 回填之后会把那个临时默认值 `DROP` 掉）。
LEGACY_COLUMNS = (
    ("jidanpu_base_url", "%s"),
    ("account", "%s"),
    ("account_name", "''"),
    ("password", "%s"),
    ("source_name", "'团团通系统'"),
    ("enabled", "false"),
    ("fast_poll_seconds", "3"),
    ("reconcile_seconds", "180"),
    ("throw_price_percent", "100"),
    ("log_cursor", "''"),
    ("input_order_kefu", "''"),
    ("last_error", "''"),
    ("created_at", "now()"),
    ("updated_at", "now()"),
    ("user_id", "%s"),
)

#: 造原始行时用户名的自增号（同一用例里造多行也不会撞 `auth_user.username` 唯一）。
_LEGACY_USERNAME_SEQ = iter(range(1, 1000))


def _insert_binding_row(*, user_id, base_url, account, password="pw-1", with_new_columns=True,
                        with_hall_sync=True, with_file_offset=True,
                        with_status_pull_seconds=True,
                        mode="password", code="", client_id="", client_secret=""):
    """用**原生 SQL** 插一行绑定（⛔ 不走 ORM：故意绕开模型层，只碰列）。

    ⚠️ `with_new_columns`：**已升级**的表要补写那 5 列新列（它们 `NOT NULL`、没有列默认值），
    **退回之后**的表则⛔ 不能写（那时列还不存在）。
    ⭐ `mode` / `code` / `client_id` / `client_secret` 四个值**由调用方给**（默认 = ①式那一套）：
    这一条正是「`save()` 覆盖不到的那些写入点」的形态（`bulk_create` / `queryset.update()` /
    `loaddata` 都不走 `save()`）—— ⇒ ⛔ **不能**在这里替调用方生成码（那就测不到「模型层补码」了）。
    ⚠️ `with_hall_sync`（D-0299）：`hall_sync_enabled` 与那 5 列**同一条口径**
    （`NOT NULL`、迁移回填后列默认值已被 DROP）⇒ 全量 schema 上**必须写** `true`；
    而「退回到 0084 之前」的重放态（`HallSyncEnabledMigrationTests`）表上还没有这一列
    ⇒ 那一支**显式传 `False`**（两态长得一模一样，⛔ 不能靠猜）。
    ⚠️ `with_file_offset`（0091）：与上面同一条口径（`NOT NULL`、迁移回填后列默认值
    已被 DROP）⇒ 全量 schema 上必须写；「退回到 0084 之前」那个重放态表上**还没有**这一列
    ⇒ 那一支也要显式传 `False`（它比 0091 更早，两件事同一次传）。
    ⚠️ `status_pull_seconds`（0095）：同一条口径——`NOT NULL` + 迁移回填后列默认值
    被 DROP ⇒ 全量 schema 上必须写一个值；而迁移**退回之后**那张表上没有这一列
    ⇒ 由 `with_status_pull_seconds` 单独控制（0084→0091 那个重放态也没有它，⛔ 别加进
    `LEGACY_COLUMNS`）。
    """
    new_columns = (
        ("mode", "%s"),
        ("client_id", "%s"),
        ("client_secret", "%s"),
        ("callback_code", "%s"),
        ("callback_count", "0"),
    )
    if with_new_columns and with_status_pull_seconds:
        #: ⭐ 0095：状态回拉的间隔（`NOT NULL`，回填值 600）。
        new_columns = (*new_columns, ("status_pull_seconds", "600"))
    if with_new_columns and with_file_offset:
        #: ⭐ 0091：已消费到的字节偏移（`BigIntegerField(default=0)`）。
        new_columns = (*new_columns, ("callback_file_offset", "0"))
    if with_new_columns and with_hall_sync:
        new_columns = (*new_columns, ("hall_sync_enabled", "true"))
    columns = list(LEGACY_COLUMNS)
    params = [base_url, account, password, user_id]
    if with_new_columns:
        #: ⚠️ 新列插在**第 10 列之后**（`LEGACY_COLUMNS` 那一段固定的位置）⇒ 占位符顺序必须是
        #: `base_url, account, password, mode, client_id, client_secret, code, user_id`
        #: （`legacy` 那一段里 `%s` 只有四个：base_url / account / password / user_id）。
        columns = columns[:10] + list(new_columns) + columns[10:]
        params = [base_url, account, password, mode, client_id, client_secret, code, user_id]
    names = [name for name, _placeholder in columns]
    placeholders = [placeholder for _name, placeholder in columns]
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {MEMBER_BINDING_TABLE} ({', '.join(names)}) "
            f"VALUES ({', '.join(placeholders)}) RETURNING id",
            params,
        )
        return cursor.fetchone()[0]


def _insert_password_row(*, base_url, account, password="pw-1"):
    """造一行「`mode="password"` + 空码」形态的行（供「①式多行空码必须放行」那条用例）。"""
    user = User.objects.create_user(
        username=f"legacy-member-{next(_LEGACY_USERNAME_SEQ)}", password="pw-a",
    )
    return _insert_binding_row(
        user_id=user.pk, base_url=base_url, account=account, password=password,
        with_new_columns=True,
    )


class MemberOfficialApiModeTests(TenantTestCase):
    """`mode` 这一列 = 「用哪套凭证」的**唯一**判据（spec §2.1），且默认必须是**登录式**。"""

    def test_mode_defaults_to_password_in_the_model(self):
        """**本任务最重要的判据之一**：`mode` 的默认值是 `"password"`（⛔ 不是 `openapi`）。

        ⚠️ 期望值写字面量 `"password"`，⛔ 不写 `JidanpuMemberBinding.Mode.PASSWORD`
        ——那是自指断言，常量被改坏时两边一起变、永远不会红（E-0024）。
        ⚠️ 这条只说明**新行**的默认值；**既有行**被迁移回填成什么，由下面那一节的重放用例钉。
        """
        self.assertEqual(JidanpuMemberBinding._meta.get_field("mode").default, "password")
        self.assertEqual(
            [value for value, _label in JidanpuMemberBinding._meta.get_field("mode").choices],
            ["password", "openapi"],
        )
        binding = _make_binding()
        self.assertEqual(binding.mode, "password")
        binding.refresh_from_db()
        self.assertEqual(binding.mode, "password")

    def test_the_new_columns_are_exactly_the_eight_ones(self):
        """⭐ 加的是**恰好 8 列**（spec §4.2 减去 `reconcile_seconds`）：多一个少一个都红。

        ⚠️ 弄红它：把 `reconcile_seconds` 也加一遍 ⇒ 集合里多一个名字（而且模型会直接报重复
        定义）；漏加 `callback_watermark_at` ⇒ 少一个。

        ⚠️ 另一条**同族**的判据：`reconcile_seconds` 只能有**一列**、且默认值仍是本线既有的
        **180**（⛔ 不许被上家线那个「兜底对账间隔(秒)」的 600 顶掉）——加第二列同名字段
        会让 Django 抛 `FieldDoesNotExist`/重复定义，而**改默认值**只能用这条挡住。
        """
        names = {field.name for field in JidanpuMemberBinding._meta.get_fields()
                 if isinstance(field, models.Field)}
        self.assertEqual(NEW_COLUMNS - names, set(), "本任务要加的列有没加上的")
        self.assertEqual(
            {name for name in names if name in NEW_COLUMNS}, NEW_COLUMNS,
            "加出来的列集合与 spec §4.2 那 8 列不是同一个集合",
        )
        # `reconcile_seconds` 只有一列，且**默认值没被改**（复用既有那一列，⛔ 不是新加一列）
        reconcile = [field for field in JidanpuMemberBinding._meta.get_fields()
                     if getattr(field, "name", "") == "reconcile_seconds"]
        self.assertEqual(len(reconcile), 1, "`reconcile_seconds` 被加了第二遍")
        self.assertEqual(reconcile[0].default, 180)
        self.assertEqual(reconcile[0].verbose_name, "对账间隔(秒)")

    def test_the_new_columns_match_the_spec_shape(self):
        """那 8 列的**可空性 / 长度 / 默认值**逐条钉死（spec §4.2 的表，形状照上家线）。

        ⚠️ 这些是「新行」的口径：`client_id` / `client_secret` / `callback_code` 是
        `blank=True, default=""`（**NOT NULL 的空串**，⛔ 不是 NULL）——「没填」与「填了空」
        在库里是同一个值；⚠️ 三个时刻列才是 `null=True`。
        """
        field = JidanpuMemberBinding._meta.get_field
        for name, max_length in (("client_id", 150), ("client_secret", 255),
                                 ("callback_code", 64)):
            column = field(name)
            self.assertEqual(column.max_length, max_length, name)
            self.assertTrue(column.blank, name)
            self.assertFalse(column.null, name)
            self.assertEqual(column.default, "", name)
        for name in ("callback_code_at", "callback_watermark_at", "callback_last_at"):
            column = field(name)
            # ⚠️ 「没有默认值」在 Django 里是哨兵 `NOT_PROVIDED`，⛔ 不是 `None`
            # （`assertIsNone(column.default)` 对这三个可空列**必红**）。
            self.assertIs(column.default, models.NOT_PROVIDED, name)
            self.assertTrue(column.null, name)
            self.assertTrue(column.blank, name)
        self.assertEqual(field("mode").max_length, 16)
        self.assertEqual(field("callback_count").default, 0)

        binding = _make_binding()
        self.assertEqual(binding.client_id, "")
        self.assertEqual(binding.client_secret, "")
        self.assertEqual(binding.callback_code, "")
        self.assertEqual(binding.callback_count, 0)
        for name in ("callback_code_at", "callback_watermark_at", "callback_last_at"):
            self.assertIsNone(getattr(binding, name), name)

    def test_openapi_mode_generates_exactly_one_code_and_stamps_it(self):
        """`mode="openapi"` ⇒ 保存时**自动**生成码 + 时间戳。

        ⚠️ 对照（同一用例里做）：①式行**一个字节都不写**（码仍为空串）——⛔ 别把生成写成
        「无条件生成」：那会让每一行登录式绑定都凭空多一串用不到的码。
        """
        binding = _make_openapi_binding()
        self.assertTrue(CALLBACK_CODE_PATTERN.match(binding.callback_code),
                        f"码的形状不对：{binding.callback_code!r}")
        self.assertIsNotNone(binding.callback_code_at)
        binding.refresh_from_db()
        self.assertTrue(CALLBACK_CODE_PATTERN.match(binding.callback_code))
        self.assertIsNotNone(binding.callback_code_at)

        password_binding = _make_binding(username="member-plain")
        self.assertEqual(password_binding.callback_code, "")
        self.assertIsNone(password_binding.callback_code_at)

    def test_the_code_hook_keys_off_mode_only(self):
        """⭐ 判「走哪条路」**只看 `mode`**：留着旧的 `client_id` 的①式行**不生成**码。

        ⚠️ 弄红它：把 `_ensure_callback_code` 里的判据写成 `if self.client_id:` 那种真值判据
        ⇒ 本条的第二个断言红（①式行凭空多一串码）。
        """
        user = User.objects.create_user(username="member-stale-client-id", password="pw-a")
        stale = JidanpuMemberBinding.objects.create(
            user=user, jidanpu_base_url="https://jidanpu-stale.test.invalid",
            account="acc-stale", password="pw-1",
            mode="password", client_id="cid-stale", client_secret="secret-stale",
        )
        self.assertEqual(stale.mode, "password")
        self.assertEqual(stale.callback_code, "")
        self.assertIsNone(stale.callback_code_at)

    def test_generation_survives_an_update_fields_write(self):
        """⭐ 走 `update_fields` 的写入点也要把码写进去。

        ⚠️ 这条盯的是一个**静默**形态：只 `set` 属性而 `update_fields` 里没有那两列 ⇒
        码看起来生成了（内存里在）、库里是空的 ⇒ 页面把地址拼出来是**半截**的、回调 404。

        ⚠️ 形态：**先有行**，再 `save(update_fields=...)`。⛔ 不能对没落库的实例直接
        `save(update_fields=...)`——那会 `ValueError: Cannot force an update in save() with no
        primary key`，测的就成了 Django 的行为而不是我们的。
        """
        existing = _make_binding(username="member-updatefields")   # 先以①式落一行
        existing.mode = "openapi"
        existing.client_id = "cid-uf"
        existing.save(update_fields=["jidanpu_base_url", "account", "password", "mode",
                                     "client_id", "updated_at"])
        existing.refresh_from_db()
        self.assertTrue(CALLBACK_CODE_PATTERN.match(existing.callback_code),
                        f"码没落库：{existing.callback_code!r}")
        self.assertIsNotNone(existing.callback_code_at)


class MemberCallbackCodePerDomainTests(TenantTestCase):
    """⭐ **一个域名一个回调码**（业务方 2026-10-09 裁定，取代原先的「一行一个码」）。

    业务方原话：「一个租户**对某一个上家域名**的回调地址应该按照**租户维度唯一**，不然多个打手
    绑定，岂不是要**填多个回调地址**」——同一个下家鸡蛋铺系统上只有**一处**「回调地址」设置项
    ⇒ 多个打手绑同一个域名时只能填同一个地址。

    本节钉四件事，**方向各不相同**（缺任何一条都会留下假绿）：

    | # | 判据 | 弄红它 |
    |---|---|---|
    | ① | 同域名的第二条②式绑定**建得上**，且**复用**第一条那个码 | 把 `_ensure_callback_code` 的复用那一支去掉 ⇒ 码不同 / 第二行被旧约束拒 |
    | ② | **不同域名**的两条②式绑定 ⇒ 码**必须不同** | 把复用写成「无条件抄第一条」/ 把生成写成固定串 |
    | ③ | 同域名下**码为空**的②式行 ⇒ 被写到**同一个码**上 | 去掉 `_propagate_code_to_domain` 那一句 |
    | ④ | ①式那条 `(域名, 账号)` 唯一约束**仍然生效**；②式同域名同账号**不再受它管** | 把 `condition=Q(mode="password")` 删掉（约束类变异要**同一次**改模型与迁移，`AGENTS.md` §5-G）｜把条件写成 `openapi` ⇒ ②式那条反向用例红 |

    ⚠️ 判据一律打在**数据库行**上（`refresh_from_db` / 原生 SQL 查回来的值），⛔ 不打内存属性。
    """

    def test_the_account_constraint_is_really_a_partial_one_on_the_database(self):
        """那条 `(域名, 账号)` 唯一约束**真的**在数据库里，而且真的带 `WHERE mode='password'`。

        ⚠️ 只断言「模型里写了 `condition=`」证明不了索引建成了（约束类变异必须同时改模型与迁移）；
        这里读的是 `pg_indexes.indexdef` —— 数据库自己怎么描述这条索引。
        """
        indexes = _index_definitions(MEMBER_BINDING_TABLE)
        self.assertIn(ACCOUNT_CONSTRAINT, indexes)
        definition = indexes[ACCOUNT_CONSTRAINT]
        self.assertIn("UNIQUE", definition.upper())
        self.assertIn("jidanpu_base_url", definition)
        self.assertIn("account", definition)
        self.assertIn("WHERE", definition.upper())
        self.assertIn("password", definition)

        constraints = connection.introspection.get_constraints(
            connection.cursor(), MEMBER_BINDING_TABLE,
        )
        self.assertIn(ACCOUNT_CONSTRAINT, constraints)

    def test_the_old_code_index_is_gone_from_the_database(self):
        """⭐ 旧的「一行一个码」那条部分唯一索引**真的**不在库里了。

        ⚠️ 为什么要有这一条：留着它的话，**同域名的第二条②式绑定照样建不上**（只是把拒绝从
        「账号」挪到了「码」）—— 而那种半新半旧的状态从「模型文件」上看不出来
        （模型删了、库里还在，只有 `pg_indexes` 说真话；`AGENTS.md` §5-C「不留半新半旧」）。
        """
        indexes = _index_definitions(MEMBER_BINDING_TABLE)
        self.assertNotIn(
            "uniq_jdp_member_callback_code", indexes,
            "旧的「一行一个码」唯一索引还在库里：同域名的第二条②式绑定会被它拒掉",
        )

    def test_a_second_openapi_binding_on_the_same_domain_reuses_the_code(self):
        """⭐ ① 同域名的第二条②式绑定 ⇒ 建得上，且它的码**与第一条相同**（⇒ 回调地址相同）。

        ⚠️ 弄红它：去掉 `_ensure_callback_code` 里「复用同域名兄弟行那个码」那一支
        ⇒ 第二行各自生成一串码（本条第二个断言红）。
        """
        first = _make_openapi_binding(username="member-openapi-a", base_url=BASE_URL_A,
                                      account="")
        second = _make_openapi_binding(username="member-openapi-b", base_url=BASE_URL_A,
                                       account="")
        self.assertNotEqual(first.pk, second.pk, "自证夹具：两行确实都建上了")
        self.assertTrue(CALLBACK_CODE_PATTERN.match(first.callback_code))
        self.assertEqual(second.callback_code, first.callback_code,
                         "同一个域名下的第二条②式绑定没有复用那个码 ⇒ 用户要填两个回调地址")
        self.assertEqual(second.callback_code_at, first.callback_code_at)
        # ⚠️ 落库的值也要对（内存里的属性对了不算：`update_fields` 那一支会只写一半）
        second.refresh_from_db()
        self.assertEqual(second.callback_code, first.callback_code)
        # ⭐ 业务方要的**最终形态**：两条绑定算出**同一个回调地址**（地址 = 域名 + 码）。
        self.assertEqual(
            jidanpu_openapi.callback_address(second).url,
            jidanpu_openapi.callback_address(first).url,
        )

    def test_openapi_bindings_on_different_domains_get_different_codes(self):
        """⭐ ② **不同域名**的两条②式绑定 ⇒ 码**必须不同**（⛔ 不是「随便都一样」）。

        ⚠️ 弄红它：把复用写成「无条件抄已经存在的第一条码」（不加 `jidanpu_base_url` 那个过滤）
        ⇒ 两个域名共用一串码 ⇒ 本条红（而①那条仍然绿 ⇒ 只有本条能抓到这种写法）。
        ⚠️ 它也**替**了旧的「同码 ⇒ IntegrityError」那条：码跨域名重复是**真危险**
        （接收器的密钥文件按码查密钥 ⇒ 后写的那一行会把前一个域名的密钥盖掉）。
        """
        a = _make_openapi_binding(username="member-openapi-a", base_url=BASE_URL_A, account="")
        b = _make_openapi_binding(username="member-openapi-b", base_url=BASE_URL_B, account="")
        self.assertTrue(CALLBACK_CODE_PATTERN.match(a.callback_code))
        self.assertTrue(CALLBACK_CODE_PATTERN.match(b.callback_code))
        self.assertNotEqual(a.callback_code, b.callback_code)

    def test_a_same_domain_row_without_a_code_gets_the_shared_code(self):
        """⭐ ③ 同域名下**码为空**的②式行 ⇒ 被写到**同一个码**上（让它们一致）。

        ⚠️ 那种行**到得了**：`save()` 之外还有写入点（`bulk_create` / `queryset.update()` /
        `loaddata` 都不走 `save()`）⇒ 不补这一句时它那一格的页面会显示不出回调地址
        （`callback_key_of` 要求码与密钥都非空）而**看不出是为什么**。
        ⚠️ 弄红它：去掉 `_propagate_code_to_domain()` 那一句 ⇒ 原生 SQL 那一行的码仍是空串。
        """
        #: 用**原生 SQL** 造一行「②式 + 空码」（绕开模型层，正是 `save()` 覆盖不到的那些写入点）。
        user = User.objects.create_user(username="member-raw-openapi", password="pw-a")
        raw_id = _insert_binding_row(
            user_id=user.pk, base_url=BASE_URL_A, account="", mode="openapi",
            code="", client_id="cid-raw", client_secret="secret-raw",
        )
        existing = JidanpuMemberBinding.objects.get(pk=raw_id)
        self.assertEqual(existing.callback_code, "", "自证夹具：那一行确实是空码")

        created = _make_openapi_binding(username="member-openapi-c", base_url=BASE_URL_A,
                                        account="")

        existing.refresh_from_db()
        self.assertTrue(CALLBACK_CODE_PATTERN.match(created.callback_code))
        self.assertEqual(
            existing.callback_code, created.callback_code,
            "同域名下原来那条空码的行没有被写上共用的码 ⇒ 它那一格永远显示不出回调地址",
        )
        self.assertEqual(existing.callback_code_at, created.callback_code_at)

    def test_password_rows_still_share_the_same_base_url_and_account_rule(self):
        """⭐ ④ **①式的行为一个字不变**：同一个域名 + 同一个账号 ⇒ 仍然不许重复绑。

        ⚠️ 弄红它：把 `condition=Q(mode="password")` 删掉（模型 + 迁移同一次改）——本条照样绿，
        所以本条**不是**它的判据；⚠️ 反过来说，把条件写成 `Q(mode="openapi")` 会让本条红
        （①式两行同账号就能建上了 = 把①式的约束放松了）。
        """
        _make_binding(username="member-plain-a", base_url=BASE_URL_A, account="acc-a")
        other = User.objects.create_user(username="member-plain-b", password="pw-b")
        with self.assertRaises(IntegrityError) as error:
            with transaction.atomic():
                JidanpuMemberBinding.objects.create(
                    user=other, jidanpu_base_url=BASE_URL_A, account="acc-a", password="pw-2",
                )
        constraint = getattr(getattr(error.exception.__cause__, "diag", None),
                             "constraint_name", "") or ""
        self.assertEqual(constraint, ACCOUNT_CONSTRAINT)

    def test_openapi_rows_are_free_of_the_account_rule(self):
        """⭐ ④ 的反向：②式**同域名 + 同账号**（含空串）⇒ **放行**（那条约束只管①式）。

        ⚠️ 弄红它：把 `condition=Q(mode="password")` 去掉 ⇒ 第二行撞 `uniq_jdp_member_account`
        ⇒ 本条红（这正是业务方报的那件事：第二条②式绑定**保存不了**）。
        ⚠️ 账号那一格在②式界面上是隐藏的（空串），所以「空串 + 空串」那一支是**真实形态**；
        「非空 + 同一个非空」那一支是「①式切②式之后留着旧用户名」的形态 ⇒ 两支都要判。
        """
        for account in ("", "acc-shared"):
            with self.subTest(account=account):
                _make_openapi_binding(username=f"member-openapi-x-{account or 'blank'}",
                                      base_url=BASE_URL_A, account=account)
                second = _make_openapi_binding(
                    username=f"member-openapi-y-{account or 'blank'}",
                    base_url=BASE_URL_A, account=account,
                )
                self.assertEqual(second.jidanpu_base_url, BASE_URL_A)

    def test_changing_the_address_realigns_the_code_to_the_new_domain(self):
        """⭐ **改地址**（D1 → D2）⇒ 本行的码**跟着新域名走**（对齐到 D2 那一串）。

        ⚠️ 不这么做，一次「改地址」就会造出「同一个域名两串码」—— 正是本次要消掉的形态，
        而它是**正常操作**（用户改自己那家鸡蛋铺的地址）⇒ 必须在这一层对齐。
        ⚠️ 代价如实登记：本行**旧的那串码**在密钥文件里**照旧留着**（那份文件只增不减），
        而本行显示的回调地址会变成 D2 那一串 —— 用户本来就要重新填一次（换的是**另一套系统**）。
        ⛔ 弄红它：把 `_stored_base_url()` 那个判断去掉（一律走「已经有一串码 ⇒ 不动它」）
        ⇒ 本行改完地址仍带 D1 的码 ⇒ 红。
        """
        d2_row = _make_openapi_binding(username="member-openapi-d2", base_url=BASE_URL_B,
                                       account="")
        d1_row = _make_openapi_binding(username="member-openapi-d1", base_url=BASE_URL_A,
                                       account="")
        self.assertNotEqual(d1_row.callback_code, d2_row.callback_code,
                            "自证夹具：两个域名各自一串码")

        d1_row.jidanpu_base_url = BASE_URL_B
        d1_row.save()

        d1_row.refresh_from_db()
        self.assertEqual(d1_row.callback_code, d2_row.callback_code,
                         "改了地址之后本行仍带旧域名那一串码 ⇒ 同一个域名两串码")

    def test_a_legacy_two_code_domain_is_not_rewritten(self):
        """⭐ 存量「**同一个域名两串码**」⇒ 两行都**不动**（那串码已经发给用户了）。

        ⚠️ 为什么**不**自动挑一串覆盖另一串：用户可能已经把其中一串填进了鸡蛋铺那侧的
        「回调地址」设置里 ⇒ 自动改写就是**替用户决定**，而后果（收不到回调）他当场看不出来。
        ⚠️ 手工对齐的路子写在 `_ensure_callback_code` 的 docstring 里（改一次地址再改回来）。
        ⛔ 弄红它：把「地址没变 ⇒ 不动它」那一支去掉（一律对齐到兄弟行）⇒ 第二行的码被改写 ⇒ 红。
        """
        first = _make_openapi_binding(username="legacy-code-a", base_url=BASE_URL_A,
                                      account="", code=LEGACY_CODE_A)
        second = _make_openapi_binding(username="legacy-code-b", base_url=BASE_URL_A,
                                       account="", code=LEGACY_CODE_B)
        self.assertEqual((first.callback_code, second.callback_code),
                         (LEGACY_CODE_A, LEGACY_CODE_B), "自证夹具：两行确实是两串码")

        first.save()
        second.save()

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.callback_code, second.callback_code),
                         (LEGACY_CODE_A, LEGACY_CODE_B),
                         "存量那两串码被自动改写了（用户已经填进鸡蛋铺的那一串会失效）")

    def test_password_rows_all_keep_an_empty_code_and_no_code_rule_applies(self):
        """⭐ ①式多行 `callback_code=""` ⇒ **照旧放行**（旧那条「码唯一」索引已经不在了）。

        ⚠️ 这两行走**原生 SQL**：先建后写都不经过模型层，测的只有「库里那条索引判据」本身。
        ⚠️ 它守着的是**既有行为**：①式行全部空码这件事在旧口径下靠「部分是 `openapi`」才合法，
        新口径下**没有任何码上的唯一性**（一个码本来就归多行共用）⇒ 更不许有任何一层拦它。
        """
        row_ids = [
            _insert_password_row(base_url=f"https://legacy-{index}.test.invalid",
                                 account=f"legacy-acc-{index}")
            for index in (1, 2)
        ]

        rows = JidanpuMemberBinding.objects.filter(pk__in=row_ids).order_by("pk")
        self.assertEqual(rows.count(), 2)
        for row in rows:
            self.assertEqual(row.mode, "password")
            self.assertEqual(row.callback_code, "")
            self.assertIsNone(row.callback_code_at)


def _business_leaf_nodes():
    """迁移图上 `business` 的**叶子**（纯元组；⛔ 不写死 `0059_*` 之类）。"""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    return [tuple(node) for node in loader.graph.leaf_nodes() if node[0] == "business"]


def _applied_migrations():
    """迁移记账表里**当前**已应用的键集合（当场读表，⛔ 不是图上的名字）。"""
    return set(MigrationRecorder(connection).applied_migrations())


def _migrate_forward_to_leaves():
    """把**当前 schema** 前放到迁移图上的**全部叶子**（⛔ 不写死任何迁移名）。

    ⭐ 为什么不能只前放到「本线那条」：合并 `main` 之后本线那条**可能不是叶子**
    ⇒ 只前放到本线那条会把库停在一个「**少别人那张表**」的状态上，而**同一进程里后面的用例**
    就在那张库上跑（症状看起来与本任务无关）。⚠️ 迁移的 DDL 与记账都是**提交**的
    （`TenantTestCase` 只回滚用例自己的行数据）⇒ 少前放一次**真的**会漏到下一个用例。

    ⚠️ 传给 `migrate()` 一律 `tuple(node)`：`leaf_nodes()` 给的是 `Node` 对象，
    而 `graph.nodes` 里装的是**纯元组**。
    """
    loader = MigrationLoader(None, ignore_no_migrations=True)
    MigrationExecutor(connection).migrate([tuple(node) for node in loader.graph.leaf_nodes()])


class MemberOfficialApiMigrationTests(TenantTestCase):
    """⭐ 本任务那条迁移**真的从头跑一遍**：既有下家绑定迁移后依然按**登录式**读（spec §4.4）。

    ## 为什么不能只断言「新行默认 password」

    存量行**不是**被 Python 默认值填的，是被 `ADD COLUMN … DEFAULT 'password'` 那条 DDL 回填的
    （Django 的 `add_field`：那个列默认值只为回填存在，随后 `DROP DEFAULT`）。
    ⇒「新行的默认值」与「既有行被填成什么」是**两件事**，前者全绿也证明不了后者。

    ## 口径：把本任务那条退掉 → 造迁移前那种行 → 正向重放

    1. `MigrationExecutor.migrate(<本任务那条的父节点>)` ⇒ 反向执行（删那 8 列 + 删索引）；
    2. 用**原生 SQL** 造行（那时表上真的没有那 8 列，正是迁移前的真实形态）；
    3. `migrate(...)` 前放回叶子 ⇒ `AddField` 对**已存在**的这些行执行
       `ADD COLUMN … DEFAULT 'password'`（PostgreSQL 会**回填既有行**）—— 真迁移走的就是这条；
    4. 断言这些行读回来仍是 `password`、且①式那条取数路径没变。
    """

    #: 本任务那条迁移之前那一版的历史模型（缓存，按进程只算一次）。
    _STATE_BEFORE = None

    def setUp(self):
        super().setUp()
        #: ⭐ **前置检查（安全网，不是判据本体）**：迁移的 DDL / 记账是**提交**的 ⇒
        #: 上一条用例若没把结构前放回叶子，**本用例就在这里**看到缺列 / 缺表。
        #: ⇒ 与 `test_all_business_leaves_are_applied_after_this_class_runs` **一对**用。
        self.assertEqual(
            sorted(node for node in _business_leaf_nodes() if node not in _applied_migrations()),
            [],
            "上一条用例收尾时没把结构前放回叶子 ⇒ 本用例跑在缺表/缺列的库上",
        )
        key = self._target_key()
        if key is None:
            #: ⚠️ 先立标志再 skip：`skipTest` 是抛异常退出的，写在它后面的行不会执行。
            self._skipped_no_target = True
            self.skipTest("迁移图上按语义找不到「给 JidanpuMemberBinding 加 mode 列」那条迁移"
                          "（被 squash / 改名 / 有两条？）⇒ 本判据另行处理")
        MigrationExecutor(connection).migrate(list(self._state_before_nodes()))

    def tearDown(self):
        # ⚠️ 还原进 `tearDown`：用例断言失败时也要把结构前放回去，否则同一进程里后面的用例
        #    会在「少 8 列 / 少别人那张表」的库上跑（症状看起来像与本任务无关的乱红）。
        # ⚠️ **无条件**前放：用例体自己前放过一次也无妨——`migrate()` 对已应用的迁移是 no-op，
        #    而漏跑一次的代价是**下一个用例红**。
        if not getattr(self, "_skipped_no_target", False):
            _migrate_forward_to_leaves()
        super().tearDown()

    def _state_before_nodes(self):
        """本任务那条迁移的父节点（= 迁移前的状态）。

        ⚠️ **故意不跟着 `_migrate_forward_to_leaves()` 改**：它要的正是「我这条**之前**」的状态。
        ⚠️ 必须 `tuple(parent)`：`graph.node_map[...].parents` 里装的是 `Node` 对象，
        而 `graph.nodes` 里装的是**纯元组** ⇒ 直接把 `Node` 交给 `project_state` / `migrate`
        会在 `forwards_plan` 的 `target not in self.nodes` 处炸。
        """
        loader = MigrationLoader(None, ignore_no_migrations=True)
        return [tuple(parent) for parent in loader.graph.node_map[self._target_key()].parents]

    def _target_key(self):
        """本任务那条迁移在图里的**键** `(app_label, name)`（当场**按语义**找，⛔ 不按序号前缀）。

        ⭐ 认它的唯一依据是它自己的 `operations`：那条
        `AddField(model_name="jidanpumemberbinding", name="mode")`。
        ⛔ 不硬编码完整文件名、⛔ 不硬编码日期后缀、⛔ 不用 `startswith("0059_")`。

        ## 为什么必须按语义找（**E-0213** 的坑，本仓实测踩过）

        `business` 里同前缀的迁移**不止一条**是常态（`0052_*` 三条、`0055_*` 两条、
        `0057_*` 两条）。用 `startswith("00xx_")` 去取，取到哪条取决于 `disk_migrations` 的
        **枚举顺序**（每个进程都不一样）⇒ 取错了那条，本线这条**压根没应用**
        ⇒ 表上没有 `mode` 列 ⇒ 后面那次结构回退会退错版本（症状飘忽、时红时绿）。
        ⚠️ 本任务尤其危险：**上家线有一条几乎同名的迁移**（都加 `mode` 列、都在 `business`）
        ⇒ 只按「有没有加 mode 列」找会一次命中**两条** ⇒ 所以才必须把 `model_name`
        一起钉进语义判据。

        被 squash / 改名 ⇒ `None` ⇒ 用例 skip（⛔ 不假绿）。
        """
        loader = MigrationLoader(None, ignore_no_migrations=True)
        candidates = [
            key for key, migration in loader.disk_migrations.items()
            if key[0] == "business" and _adds_member_mode_column_operation(migration) is not None
        ]
        #: ⚠️ 恰好一条才认它：两条说明结构变了 ⇒ 宁可 skip。
        return candidates[0] if len(candidates) == 1 else None

    def _state_before(self):
        """本任务那条迁移之前那一版的历史模型。"""
        if MemberOfficialApiMigrationTests._STATE_BEFORE is None:
            loader = MigrationLoader(None, ignore_no_migrations=True)
            MemberOfficialApiMigrationTests._STATE_BEFORE = loader.project_state(
                self._state_before_nodes()
            )
        return MemberOfficialApiMigrationTests._STATE_BEFORE

    def _legacy_rows(self, base_urls):
        """在**结构已退回去**的表上造「迁移前那种行」。

        ⚠️ 两条都**实测**踩过（上家线同形用例），缺一条就假红：
          ① 反向迁移会把依赖的 `auth` app 一起退掉 ⇒ 那时 `User.objects.create_user(...)`
             报 `NotNullViolation: null value in column "last_login"` ⇒ User 也必须用原生 SQL 插；
          ② 表上此刻**没有**那 8 列 ⇒ 行只写 `LEGACY_COLUMNS`。
        """
        historical = self._state_before().apps.get_model("business", "JidanpuMemberBinding")
        self.assertNotIn(
            "mode", [field.name for field in historical._meta.get_fields()],
            "历史模型里已经有 mode ⇒ 这不是本任务那条迁移之前的状态，本用例无效",
        )
        row_ids = []
        for index, base_url in enumerate(base_urls, start=1):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s AND column_name = 'mode'",
                    [connection.schema_name, MEMBER_BINDING_TABLE],
                )
                self.assertIsNone(cursor.fetchone(), "结构没退干净：mode 列还在（那本用例无效）")
                cursor.execute(
                    "INSERT INTO auth_user (password, is_superuser, username, first_name, "
                    " last_name, email, is_staff, is_active, date_joined, last_login) "
                    "VALUES ('', false, %s, '', '', '', false, true, now(), now()) RETURNING id",
                    [f"hist-member-{index}"],
                )
                user_id = cursor.fetchone()[0]
            row_ids.append(_insert_binding_row(
                user_id=user_id, base_url=base_url, account=f"hist-acc-{index}",
                with_new_columns=False,
            ))
        return row_ids

    def test_the_migration_declares_password_as_the_default(self):
        """迁移文件里 `mode` 那条 `AddField` 的默认值是 `'password'`（存量行就是按它回填的）。

        ⚠️ 弄红它：把模型默认值改成 `"openapi"` 并**同一次**重新生成迁移（模型与迁移一起变）
        ⇒ 本条与下面那条**同时**红；只改模型不改迁移 ⇒ 本条红 —— 这正是「模型与迁移各自漂移」
        那个静默形态的判据（新库建出来与旧库升上来的语义会不同）。
        """
        self.assertEqual(
            _migration_add_field_default("jidanpumemberbinding", "mode"), "password",
        )

    def test_existing_rows_are_still_password_mode_after_the_migration(self):
        """⭐ **本任务最重要的判据**：重放迁移之后，既有的“登录式”行**依然是登录式**。

        ⚠️ 弄红它：把 `mode` 的 `default` 换成 `"openapi"`（模型 + 迁移两处一起改，
        照 `AGENTS.md` §5-G 的约束类纪律：只改一处读数就不指向任何单条改动）
        ⇒ 回填把这两行填成 `openapi` ⇒ 下面第一条断言红。
        ⚠️ 顺带钉住两件事：①式行**不需要**回调码；迁移里那条**部分**唯一索引对多行空码**放行**
        （它要是被写成全表唯一，前放这一步的第二行回填就会以 `IntegrityError` 炸在这里）。
        ⚠️ 判据打**数据库行**（`refresh_from_db` 后读模型），⛔ 不是读模型默认值。
        """
        rows = self._legacy_rows([BASE_URL_A, BASE_URL_B])
        self.assertEqual(len(rows), 2)

        #: ⚠️ **前放回到全部叶子**（⛔ 不是 `migrate([self._target_key()])`）：本线那条可能不是叶子
        #: ⇒ 只前放到它会把库停在「少别人那张表」的状态，而**同一进程里后面的用例**就在那张库上跑。
        _migrate_forward_to_leaves()

        #: ⭐ 原判据的另一半（与 `_legacy_rows` 里那条 `mode` 列**不在**的断言对称）：
        #: 「退回去 ⇒ `mode` 列真的不在」↔「前放 ⇒ `mode` 列真的在」。
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s AND column_name = 'mode'",
                [connection.schema_name, MEMBER_BINDING_TABLE],
            )
            self.assertEqual(cursor.fetchone(), ("mode",), "前放之后 mode 列仍然不在 ⇒ 迁移没被应用")

        for index, row_id in enumerate(rows, start=1):
            current = JidanpuMemberBinding.objects.get(pk=row_id)
            self.assertEqual(current.mode, "password", f"第 {index} 行被迁移改成了别的对接方式")
            self.assertEqual(current.client_id, "")
            self.assertEqual(current.client_secret, "")
            self.assertEqual(current.callback_code, "")
            self.assertIsNone(current.callback_code_at)
            self.assertEqual(current.callback_count, 0)
            #: ⚠️ `reconcile_seconds` 是**既有**那一列（默认 180），不是本任务新加的
            self.assertEqual(current.reconcile_seconds, 180)
            #: ⚠️ 承重面：登录式那条路读的还是「地址 + 账号 + 密码」（⛔ 没被新列挤掉）
            self.assertFalse(
                jidanpu_member.credentials_missing(current),
                "迁移之后登录式那条路的凭据被判成不全",
            )

    def test_one_existing_row_alone_is_still_password_after_the_migration(self):
        """⭐ 同上那条的**单行版**：把「回填写成了别的值」这件事判成一条**干净的 FAIL**。

        ## 为什么单行版必须与多行版**并存**（实测的分工）

        多行版（上面那条）在 `mode` 默认值被改坏时会以 **`ERROR`** 收场：
        两行都被回填成 `openapi` + 空码 ⇒ **前放那一步建部分唯一索引时直接炸**
        （实测 `IntegrityError: could not create unique index "uniq_jdp_member_callback_code"
        / DETAIL: Key (callback_code)=() is duplicated.`）⇒ 它是**更强**的因果读数，
        但它红在**迁移那一步**、没走到「行读回来是什么」。

        单行版只造**一行**：索引建得上 ⇒ 前放成功 ⇒ 判据落在**行读数**上
        （`current.mode` 是不是 `"password"`）。两条各自能红、红的层不同 ⇒ 都要留。
        """
        rows = self._legacy_rows([BASE_URL_A])
        self.assertEqual(len(rows), 1)
        _migrate_forward_to_leaves()

        current = JidanpuMemberBinding.objects.get(pk=rows[0])
        self.assertEqual(current.mode, "password", "被迁移回填成了别的对接方式")
        self.assertEqual(current.client_id, "")
        self.assertEqual(current.client_secret, "")
        self.assertEqual(current.callback_code, "")
        self.assertFalse(jidanpu_member.credentials_missing(current))

    def test_the_target_key_is_the_migration_that_adds_the_member_mode_column(self):
        """⭐ **防 E-0213 复发**：`_target_key()` 找到的那条**确实就是**给**下家**表加 `mode` 那条。

        ## 为什么必须单独钉这一条

        `business` 里有**两条**迁移声明了 `AddField(..., name="mode")`：上家线的 `0057_*`
        与本任务的 `0059_*`。语义判据只要漏掉 `model_name` 那一维，就会一次命中两条
        （`_target_key()` 回 `None` ⇒ skip ⇒ **假绿**），或者按枚举顺序取到**上家那条**
        ⇒ 本类的结构回退会把库退到错的地方（症状飘忽）。

        ⚠️ 上面那两条行读数用例**盖不住**这个缺陷：它们断言的是「本线那条被应用之后」的行为，
        而失效形态是「**本线那条没被选中**」——那一步没有判据（本条补的就是它）。

        ## 弄红它

        把 `_adds_member_mode_column_operation` 里的 `operation.model_name ==
        "jidanpumemberbinding"` 去掉（只按 `name == "mode"` 找）⇒ 候选变成两条
        ⇒ `_target_key()` 回 `None` ⇒ 本条红（`assertIsNotNone` 那条；`setUp` 里也会 skip）。
        """
        loader = MigrationLoader(None, ignore_no_migrations=True)
        key = self._target_key()
        self.assertIsNotNone(key, "按语义找不到给下家表加 mode 列那条迁移 ⇒ 本判据无效")

        #: ⭐ **恰好集合**：全 `business` 里只有一条迁移声明了「给下家表加 mode 列」这条 `AddField`
        declaring = sorted(
            key for key, migration in loader.disk_migrations.items()
            if key[0] == "business" and _adds_member_mode_column_operation(migration) is not None
        )
        self.assertEqual([key], declaring, "`_target_key()` 取到的那条不是声明了这条 AddField 的那条")
        #: ⚠️ 对照：**上家**那条同名操作**不在**这个集合里（两条表各自一条，⛔ 不混）
        self.assertEqual(len(declaring), 1)

        operation = _adds_member_mode_column_operation(loader.disk_migrations[key])
        self.assertEqual(operation.model_name, "jidanpumemberbinding")
        self.assertEqual(operation.name, "mode")
        self.assertEqual(operation.field.default, "password")

    def test_the_migration_declares_the_partial_condition_on_mode(self):
        """迁移里那条 `AddUniqueConstraint` 的条件**带 `mode`**（⛔ 不是无条件唯一）。

        ⚠️ 弄红它：把模型里 `condition=Q(mode="openapi")` 去掉并重新生成迁移
        （约束类变异必须**同一次**改模型与迁移）⇒ 本条红。
        """
        loader = MigrationLoader(None, ignore_no_migrations=True)
        key = self._target_key()
        self.assertIsNotNone(key, "找不到本任务那条迁移 ⇒ 本判据无效")
        operations = [
            operation for operation in loader.disk_migrations[key].operations
            if type(operation).__name__ == "AddConstraint"
        ]
        self.assertEqual(len(operations), 1)
        constraint = operations[0].constraint
        self.assertEqual(constraint.name, CALLBACK_CODE_CONSTRAINT)
        self.assertEqual(tuple(constraint.fields), ("callback_code",))
        self.assertIsNotNone(constraint.condition, "条件没了 ⇒ 那是**全表**唯一（多行空码会撞）")
        self.assertEqual(str(constraint.condition), str(models.Q(("mode", "openapi"))))

    def test_all_business_leaves_are_applied_after_this_class_runs(self):
        """⭐ **前放要回到叶子**：本类跑完之后 `business` 的**所有叶子都已在库上**。

        ⚠️ 迁移的 DDL 与记账都是**提交**的（`TenantTestCase` 只回滚用例自己的行数据）
        ⇒ 「少前放」**真的**会漏到同一进程里后面的用例。

        ## 判据打在**库的终态**上（⛔ 不是只读图上的名字）

        两条独立读数，缺一条都可能是假绿：
          ① **记账表**：`business` 的每个叶子都已在 `django_migrations` 里；
          ② **叶子产物的实际列 / 表**：`mode` 列（本任务那条迁移加的）＋
             `business_feedback` 表（**另一条** `0057` 的产物，用来分辨「只前放到本线那条」）。
        """
        leaves = _business_leaf_nodes()
        #: ⚠️ 非空是**前提**（空集合会让下面那条断言恒真 ⇒ 假绿）。
        self.assertTrue(leaves, "`business` 在图上没有叶子 ⇒ 本判据没意义")

        _migrate_forward_to_leaves()

        applied = _applied_migrations()
        self.assertEqual(
            sorted(node for node in leaves if node not in applied), [],
            "本类跑完之后 `business` 有叶子没被应用 ⇒ 同一进程里后面的用例跑在缺表的库上",
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s AND column_name = 'mode'",
                [connection.schema_name, MEMBER_BINDING_TABLE],
            )
            self.assertEqual(cursor.fetchone(), ("mode",),
                             "`mode` 列不在 ⇒ 加它的那条迁移没被应用（`_target_key()` 又取错了？）")
            cursor.execute("SELECT to_regclass(%s)", [f"{connection.schema_name}.business_feedback"])
            self.assertIsNotNone(
                cursor.fetchone()[0],
                "`business_feedback` 不在 ⇒ 前放只到「本线那条」、没回叶子",
            )


class HallSyncEnabledMigrationTests(TenantTestCase):
    """⭐ D-0299 那条迁移**真的重放一遍**：存量行被 `ADD COLUMN … DEFAULT true` 回填成 True。

    ## 为什么不能只断言「新行默认 True」

    「新行的默认值」由 Python 模型层给；**存量行**是被迁移的 DDL 回填的——两份各自可以漂移
    （改了模型忘了同一次改迁移 ⇒ 新库与升上来的旧库语义不同）。⇒ 口径照
    `MemberOfficialApiMigrationTests`：把本任务那条退掉 → 造迁移前那种行 → 正向前放。

    ⚠️ 退/前放的都是**提交过的 DDL** ⇒ tearDown **无条件**前放回叶子（用例红也不能把
    缺列的库漏给同进程后面的用例；前置检查与收尾检查成对，照那个类的既有手法）。
    """

    def setUp(self):
        super().setUp()
        self.assertEqual(
            sorted(node for node in _business_leaf_nodes() if node not in _applied_migrations()),
            [],
            "上一条用例收尾时没把结构放回叶子 ⇒ 本用例跑在缺列/缺表的库上",
        )
        self._skipped_no_target = self._target_key() is None
        if self._skipped_no_target:
            self.skipTest(
                "迁移图上按语义找不到「给 JidanpuMemberBinding 加 hall_sync_enabled」那条迁移"
                "（被 squash / 改名 / 有两条？）⇒ 本判据另行处理"
            )
        MigrationExecutor(connection).migrate(list(self._state_before_nodes()))

    def tearDown(self):
        if not self._skipped_no_target:
            _migrate_forward_to_leaves()
        super().tearDown()

    def _target_key(self):
        """那条迁移在图上的键（**按语义**找：`AddField(jidanpumemberbinding, hall_sync_enabled)`）。

        ⛔ 不按文件名 / 序号前缀找（E-0213：同前缀迁移不止一条是常态，取错会退错结构）。
        """
        loader = MigrationLoader(None, ignore_no_migrations=True)
        for key, migration in loader.disk_migrations.items():
            if key[0] != "business":
                continue
            for operation in migration.operations:
                if (type(operation).__name__ == "AddField"
                        and operation.model_name == "jidanpumemberbinding"
                        and operation.name == "hall_sync_enabled"):
                    return key
        return None

    def _state_before_nodes(self):
        """那条迁移的**父节点**（= 加列之前的状态）。⚠️ `graph.node_map` 装的是 `Node`，
        `migrate()` 要**纯元组**（照 `MemberOfficialApiMigrationTests._state_before_nodes`）。"""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        return [tuple(parent) for parent in loader.graph.node_map[self._target_key()].parents]

    def test_the_migration_declares_true_as_the_default(self):
        """迁移里那条 `AddField` 的 default 就是 **True**（字面值判据，⛔ 不是「有默认就行」）。

        判别力：把它写成 False（或两处只改一处）⇒ 本用例红——那会把**全部存量绑定**
        在升库那一刻静默变成「不同步大厅」，正好与 D-0299 的「存量行为不变」相反。
        """
        self.assertIs(
            _migration_add_field_default("jidanpumemberbinding", "hall_sync_enabled"), True,
        )
        self.assertIs(JidanpuMemberBinding._meta.get_field("hall_sync_enabled").default, True)

    def test_existing_rows_are_backfilled_true(self):
        """**终态判据**：迁移前就在的行，升完读回来 `hall_sync_enabled` 是 **True**。

        判别力：迁移写成 `default=False` / 漏掉那条 AddField ⇒ 本用例红（前者回填 False、
        后者 `_target_key()` 找不到而在 setUp 里 skip——skip 不是绿，本文件前置检查会看见）。
        """
        user = User.objects.create_user(username="hall-sync-legacy-a", password="pw-a")
        row_id = _insert_binding_row(
            user_id=user.pk,
            base_url="https://jidanpu-hallsync.test.invalid",
            account="acc-hallsync",
            # ⚠️ 此刻表上**还没有** `hall_sync_enabled` 这一列（setUp 刚退到 0084 之前）
            #    ⇒ 显式不写它（全量 schema 的插入才要补 `true`，见那个参数的 docstring）。
            # ⚠️ 同理：这个重放态（0084 之前）也**早于 0091** ⇒ `callback_file_offset` 列还不存在。
            with_new_columns=True,
            with_hall_sync=False,
            with_file_offset=False,
            #: ⭐ 0095比这个重放态更晚 ⇒ 那一版表上还没有 `status_pull_seconds`。
            with_status_pull_seconds=False,
        )
        # ⚠️ **先烧掉在途的延迟触发器再前放**（PG16 实测，本机探针复现）：
        #    上面那笔原生 INSERT 的外键检查是 `DEFERRABLE INITIALLY DEFERRED`（Django FK 全是）
        #    ⇒ 事件挂到事务收口才发；而 `ADD COLUMN … DEFAULT true NOT NULL` 在「同事务里有
        #    未发的在途触发器事件」时会被 PG 拒（`ObjectInUse: pending trigger events`）。
        #    `SET CONSTRAINTS ALL IMMEDIATE` 把挂起的外键检查**当场烧掉**（user 行已存在，
                #    检查必过）⇒ 事件清空，ADD COLUMN 照常走。探针与依据：本用例注释。
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        _migrate_forward_to_leaves()
        row = JidanpuMemberBinding.objects.get(pk=row_id)
        self.assertIs(row.hall_sync_enabled, True)
