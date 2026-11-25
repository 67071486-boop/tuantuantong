"""上家鸡蛋铺线（客服个人绑定）三张新表的形状、默认值与约束（计划 ；spec §4.1–§4.3）。

    本模块只钉**表本身的形状**（表名 / 列名 / 默认值 / `on_delete` / 唯一约束 / `blank`·`null` 语义）：
    - `JidanpuUpstreamBinding`：一个客服一份（`user` 唯一 ⇒ 第二次 `create` 撞约束）；
    同一套上家里同一个账号不重复绑（`(jidanpu_base_url, account)` 唯一）；两个间隔列的默认值；
    - `JidanpuUpstreamOrderLink`：`(binding, order)` 唯一 + `(binding, upstream_order_id)` 唯一；
    - `JidanpuUpstreamHallOrder`：镜像键 = `(upstream_base_url, upstream_order_id)`（⚠️ **带地址**：
    两家上家的单号各自生成、会撞）。

    ⚠️ 判据打**数据库行**（`AGENTS.md` §5-G）：靠 `IntegrityError` / `ProtectedError` 与字段值断言，⛔ 不打 logger。
    ⚠️ 每条唯一约束都配一条**反向用例**（换个维度就该放行）——只钉「重复会炸」证明不了约束是**复合**的：
    把 `(jidanpu_base_url, account)` 误写成「`account` 单列唯一」、把 `(binding, upstream_order_id)`
    误写成「`upstream_order_id` 全局唯一」、把 `(upstream_base_url, upstream_order_id)`
    误写成「`upstream_order_id` 全局唯一」，正向用例照样全绿。
    ⚠️ 默认值断言写**字面量** `30` / `60`（spec §10 / §1.3 D-b 的值；⭐ 2026-10-25 由 300 缩到 120、
    2026-11-02 D-0363 再缩到 30，均为业务方拍板），⛔ 不写 `== UPSTREAM_HALL_POLL_SECONDS`
    ——那是自指断言：常量被改坏时两边一起变、**永远不会红**（E-0024）。
    ⚠️ 三条 `on_delete` **刻意各档不同**（spec §8.4 末尾的补句）：断言按**行为**（删绑定 ⇒ 链路没、
    镜像行活下来）与**属性**两头钉，⛔ 不「统一」。
    ⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `acc-a`、
    密码 `pw-1`；⛔ 零真实网络——本模块只碰 ORM。

    ⚠️ **已知空档（Low； 补丁 Step 4 登记，⛔ 不为此改实现）**——权威清单在
    `docs/plans/2026-09-28-upstream-jidanpu-sync.md` 的「 补丁」Step 4 表，此处留一份指路：
    - **L1** `upstream_payload`（JSON）**没有 round-trip 用例**（存进去再读出来是否原样；它是原样留痕字段）；
    - **L2** 8 个文本 / 金额列**没有长度与精度边界用例**（超长 / 越界；与 spec §13.1-R5 同族，留给后续按需补）；
    - **L3** 三张表**都没有 `Meta.ordering` 用例**（本线读路径都显式 `order_by`，ordering 不是承重逻辑）；
    - **L4** 只读属性 `base_url` **赋值应抛 `AttributeError`** 没有用例；
    - **L5** 唯一约束**不做归一化**（只差大小写 / 空白可重复绑，**有意与 P2 同形**，⛔ 不擅自加归一化）。
"""
import re

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import ProtectedError
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu
from apps.business.models import (
    Game,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    JidanpuUpstreamOrderLink,
    Order,
)

BASE_URL_A = "https://jidanpu-upstream-a.test.invalid"
BASE_URL_B = "https://jidanpu-upstream-b.test.invalid"
REMOTE_ORDER_ID = "upstream-order-0001"

#: 期望的表名（⛔ 独立的期望值，不是从被测模型派生出来的）。
BINDING_TABLE = "business_jidanpuupstreambinding"
LINK_TABLE = "business_jidanpuupstreamorderlink"
HALL_TABLE = "business_jidanpuupstreamhallorder"

BINDING_COLUMNS = {
    "id",
    "user_id",
    "jidanpu_base_url",
    "account",
    "account_name",
    #: ⭐ 人填的「上家系统名称」（迁移 `0123_jidanpuupstreambinding_name.py`）；
    #: 与 `account_name`（同步引擎缓存的只读显示名）是两列。
    "name",
    "password",
    "enabled",
    #: ⭐ 发单价的勾兑百分比（业务方口径修订，`D-0173`；迁移
    #: `0053_jidanpuupstreambinding_hall_price_percent.py`）。
    #: ⚠️ **漏了这一行的后果**：模型与迁移都有这一列，而本用例的期望集（**独立的**字面量、
    #: ⛔ 不从被测模型派生）没跟上 ⇒ 全量后端套件**红**（本任务执行期实测到的既有红；
    #: 不是本任务引入的， 一个字节都没动模型）。
    "hall_price_percent",
    "game_rules",
    "hall_poll_seconds",
    "my_orders_poll_seconds",
    "identity_revision",
    "hall_cursor",
    "my_orders_progress",
    "last_run_at",
    "last_ok_at",
    "last_error",
    "last_error_at",
    "checked_at",
    #: ⭐ 官方 Open Platform API 那一组（spec `docs/specs/2026-10-08-jidanpu-official-api-binding-design.md`
    #: §4.2；迁移 `0057_jidanpuupstreambinding_callback_code_and_more.py`）。九列**全部**可空或带默认值
    #: ⇒ 迁移只做 `ADD COLUMN`、无需回填（§4.4 第 1 步）。
    "mode",
    "client_id",
    "client_secret",
    "callback_code",
    "callback_code_at",
    "callback_watermark_at",
    "callback_last_at",
    "callback_count",
    "reconcile_seconds",
    #: ⭐ 订单来源字典项的 FK 列（D-0261，迁移 `0074_jidanpuupstreambinding_order_source`）。
    #: ⚠️ 与 `hall_price_percent` 那条同一条纪律：模型与迁移都有这一列，而本期望集是**独立的**
    #: 字面量（⛔ 不从被测模型派生）⇒ 迁移一落本网必红（本次执行期先红实测读数：
    #: `Items in the first set but not the second: 'order_source_id'`），必须**同一次**更新（E-0175 同族）。
    "order_source_id",
    #: ⭐ 已消费到的字节偏移（生产性能缺陷修复，迁移
    #: `0091_callback_file_offset.py`）。与上面两条同一条纪律：模型 / 迁移 / **本期望集**
    #: 三处必须**同一次**改（先红实测：`Items in the first set but not the second:
    #: 'callback_file_offset'`）。
    "callback_file_offset",
    "created_at",
    "updated_at",
}
LINK_COLUMNS = {
    "id",
    "binding_id",
    "order_id",
    "upstream_order_id",
    "retired_at",
    "in_hall",
    "completed_submitted_at",
    "terminated_at",
    "last_in_at",
    "last_partial_probe_at",
    "last_out_at",
    #: ⭐ 三连「查不到」的计数（判「上家已移除接手人」用）。
    "miss_count",
    "miss_since",
    "miss_at",
}
HALL_COLUMNS = {
    "id",
    "upstream_base_url",
    "upstream_order_id",
    "owner_binding_id",
    "status",
    "game_id",
    "remote_game_area",
    "remote_game_server",
    "project_text",
    "remote_remark",
    "remote_role_name",
    "remote_rec_fee",
    "hall_price_percent_at_sync",
    "remote_created_at",
    "upstream_payload",
    "claiming_by_id",
    "claiming_at",
    "promoted_order_id",
    "gone_at",
    "last_error",
    "first_seen_at",
    "last_seen_at",
}


def _make_binding(*, username="upstream-a", base_url=BASE_URL_A, account="acc-a"):
    user = User.objects.create_user(username=username, password="pw-a")
    return JidanpuUpstreamBinding.objects.create(
        user=user, jidanpu_base_url=base_url, account=account, password="pw-1",
    )


class UpstreamBindingShapeTests(TenantTestCase):
    def test_defaults_match_the_spec(self):
        """两个间隔列落库的默认值 = 大厅 **30**（⭐ 2026-10-25 由 300 缩到 120；
    2026-11-02 D-0363 再缩到 30）/ 我的订单 60。

    ⛔ 不写常量名（模块 docstring 的口径：字面值才是契约，E-0024）。
"""
        binding = _make_binding()
        self.assertEqual(binding.hall_poll_seconds, 30)
        self.assertEqual(binding.my_orders_poll_seconds, 60)
        self.assertFalse(binding.enabled)
        self.assertEqual(binding.account_name, "")
        # ⭐ 人填的「上家系统名称」默认空串（选填；与只读的 account_name 是两列）。
        self.assertEqual(binding.name, "")
        self.assertEqual(binding.hall_cursor, "")
        self.assertEqual(binding.last_error, "")
        for field in ("last_run_at", "last_ok_at", "last_error_at", "checked_at"):
            self.assertIsNone(getattr(binding, field), field)
        #: ⭐ D-0261：来源默认**未配置**（fail-soft：不配 ⇒ 来的单照旧无来源）。
        self.assertIsNone(binding.order_source_id)
        self.assertIsNotNone(binding.created_at)
        self.assertIsNotNone(binding.updated_at)

    def test_interval_columns_are_non_negative_integers(self):
        """两个间隔列 = `PositiveIntegerField`（非负整数）：`0` 存得进去（它是「配置写坏」那个边界，
    兜底在闸门那一侧，⛔ 不在列上加校验器）；`-1` 必须在**数据库**层被拒。"""
        binding = _make_binding()
        for field in ("hall_poll_seconds", "my_orders_poll_seconds"):
            self.assertIsInstance(
                JidanpuUpstreamBinding._meta.get_field(field), models.PositiveIntegerField, field,
            )

        binding.hall_poll_seconds = 0
        binding.my_orders_poll_seconds = 0
        binding.full_clean()          # ⛔ 校验器不许把 0 判非法（否则兜底那一档永远走不到）
        binding.save()
        binding.refresh_from_db()
        self.assertEqual(binding.hall_poll_seconds, 0)
        self.assertEqual(binding.my_orders_poll_seconds, 0)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                JidanpuUpstreamBinding.objects.create(
                    user=User.objects.create_user(username="upstream-neg", password="pw-a"),
                    jidanpu_base_url=BASE_URL_B, account="acc-neg", password="pw-1",
                    hall_poll_seconds=-1,
                )

    def test_user_can_hold_multiple_rows(self):
        """⛔ **2026-10-18 改准（D-0256 / E-0255）**：本用例原名 `test_one_row_per_user`，
    原判据「一个客服同时只有一份绑定（第二次 `create` 必须撞唯一约束）」随
    「按用户单行 → 租户多行」**整体作废**——`user` 列已从 `OneToOneField` 改
    `ForeignKey`（原库级唯一约束物理挡住「同一客服绑第二个上家」，正是 E-0255
    的病根：换第二个上家只能静默覆盖本人那一行）。

    现钉**新形状**：同一客服可以有多行（`地址+账号` 各不相同）；
    「同一套 `地址+账号` 只许一行（跨客服也一样）」由下一条
    `test_same_address_same_account_only_once` 继续钉住。
"""
        binding = _make_binding()
        second = JidanpuUpstreamBinding.objects.create(
            user=binding.user, jidanpu_base_url=BASE_URL_B, account="acc-b", password="pw-2",
        )
        self.assertNotEqual(second.pk, binding.pk)
        self.assertEqual(
            JidanpuUpstreamBinding.objects.filter(user=binding.user).count(), 2
        )

    def test_same_address_same_account_only_once(self):
        """同一套上家地址上的同一个账号不重复绑（不同客服也不行）。"""
        _make_binding(username="upstream-a", base_url=BASE_URL_A, account="acc-a")
        other = User.objects.create_user(username="upstream-b", password="pw-b")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                JidanpuUpstreamBinding.objects.create(
                    user=other, jidanpu_base_url=BASE_URL_A, account="acc-a", password="pw-2",
                )

    def test_same_account_on_another_base_url_is_allowed(self):
        """账号唯一是**跟着地址走的**：另一家上家上的同名账号必须放行（否则约束被写成了单列唯一）。"""
        _make_binding(username="upstream-a", base_url=BASE_URL_A, account="acc-a")
        other = User.objects.create_user(username="upstream-b", password="pw-b")
        binding = JidanpuUpstreamBinding.objects.create(
            user=other, jidanpu_base_url=BASE_URL_B, account="acc-a", password="pw-2",
        )
        self.assertEqual(binding.account, "acc-a")

    def test_base_url_property_enables_the_shared_client_factory(self):
        """⛔ 不给本线写第三个建连函数：地址列命名为 `jidanpu_base_url`（`open_client_with_http`
    读的就是它）+ 只读属性 `base_url`（`open_client` / `credentials_of` 读那个）
    ⇒ 两个既有建连函数**都**直接可用（读码：`apps/business/jidanpu.py` 的两个函数读不同字段名）。"""
        binding = _make_binding()
        self.assertEqual(binding.base_url, binding.jidanpu_base_url)
        self.assertEqual(
            jidanpu.credentials_of(binding), (binding.jidanpu_base_url, "acc-a", "pw-1")
        )
        # ⚠️ `full_clean()` 不该因为多了一个同名属性而报错（属性不参与模型校验）。
        binding.full_clean()

    def test_blank_fields_accept_empty_strings_and_the_row_round_trips(self):
        """`blank=True` 的文本列默认空串、写进去读回来还是空串（⛔ 不是 NULL）。"""
        binding = _make_binding()
        binding.account_name = ""
        binding.name = ""
        binding.last_error = ""
        binding.full_clean()
        binding.refresh_from_db()
        self.assertEqual(binding.account_name, "")
        self.assertEqual(binding.name, "")
        self.assertEqual(binding.last_error, "")


class UpstreamOrderLinkShapeTests(TenantTestCase):
    def setUp(self):
        self.binding = _make_binding(username="upstream-a")
        self.game = Game.objects.create(name="合成游戏甲")
        self.order_a = Order.objects.create(game=self.game, order_no="20260928000000009101")
        self.order_b = Order.objects.create(game=self.game, order_no="20260928000000009102")

    def test_defaults_match_the_spec(self):
        link = JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=self.order_a, upstream_order_id=REMOTE_ORDER_ID,
        )
        self.assertFalse(link.in_hall)
        for field in ("completed_submitted_at", "terminated_at", "last_in_at", "last_out_at"):
            self.assertIsNone(getattr(link, field), field)

    def test_is_terminated_tracks_terminated_at(self):
        link = JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=self.order_a, upstream_order_id=REMOTE_ORDER_ID,
        )
        self.assertFalse(link.is_terminated)
        link.terminated_at = timezone.now()
        link.save()
        self.assertTrue(link.is_terminated)

    def test_one_link_per_binding_and_order(self):
        """同一绑定里一张我方订单只有一条链路。"""
        JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=self.order_a, upstream_order_id=REMOTE_ORDER_ID,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                JidanpuUpstreamOrderLink.objects.create(
                    binding=self.binding, order=self.order_a, upstream_order_id="upstream-order-0002",
                )

    def test_remote_order_id_unique_within_binding(self):
        """同一绑定里一个上家单号只能挂一条链路。"""
        JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=self.order_a, upstream_order_id=REMOTE_ORDER_ID,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                JidanpuUpstreamOrderLink.objects.create(
                    binding=self.binding, order=self.order_b, upstream_order_id=REMOTE_ORDER_ID,
                )

    def test_two_addresses_may_share_a_remote_order_id(self):
        """⛔ 远端单号的唯一键**必须带 `binding`**：两家上家的单号各自生成，会撞。

    全局唯一的话这里第二行会 `IntegrityError`（那是「像是我们写错了」的形态）。
"""
        other_binding = _make_binding(
            username="upstream-b", base_url=BASE_URL_B, account="acc-b"
        )
        a = JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=self.order_a, upstream_order_id=REMOTE_ORDER_ID,
        )
        b = JidanpuUpstreamOrderLink.objects.create(
            binding=other_binding, order=self.order_b, upstream_order_id=REMOTE_ORDER_ID,
        )
        self.assertNotEqual(a.pk, b.pk)


class UpstreamHallOrderShapeTests(TenantTestCase):
    def setUp(self):
        self.binding = _make_binding(username="upstream-a")
        self.game = Game.objects.create(name="合成游戏甲")

    def _mirror_kwargs(self, **overrides):
        kwargs = dict(
            upstream_base_url=BASE_URL_A,
            upstream_order_id=REMOTE_ORDER_ID,
            first_seen_at=timezone.now(),
        )
        kwargs.update(overrides)
        return kwargs

    def test_defaults_match_the_spec(self):
        mirror = JidanpuUpstreamHallOrder.objects.create(**self._mirror_kwargs())
        self.assertEqual(mirror.status, "listed")
        self.assertIsNone(mirror.owner_binding)
        self.assertIsNone(mirror.game)
        self.assertIsNone(mirror.remote_rec_fee)
        self.assertIsNone(mirror.remote_created_at)
        self.assertIsNone(mirror.upstream_payload)
        self.assertIsNone(mirror.claiming_by)
        self.assertIsNone(mirror.claiming_at)
        self.assertIsNone(mirror.promoted_order)
        self.assertIsNone(mirror.gone_at)
        self.assertIsNone(mirror.last_seen_at)
        for field in ("remote_game_area", "remote_game_server", "project_text",
                      "remote_remark", "remote_role_name", "last_error"):
            self.assertEqual(getattr(mirror, field), "", field)
        self.assertIsNotNone(mirror.first_seen_at)

    def test_status_enum_values_match_the_spec(self):
        """五个状态逐字钉死（spec §4.3）——往返用例挡不住「删掉一个成员」。"""
        self.assertEqual(
            list(JidanpuUpstreamHallOrder.Status.values),
            ["listed", "claiming", "promoted", "gone", "skipped"],
        )
        self.assertEqual(JidanpuUpstreamHallOrder._meta.get_field("status").default, "listed")

    def test_mirror_is_keyed_by_address_and_order_id(self):
        JidanpuUpstreamHallOrder.objects.create(**self._mirror_kwargs())
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                JidanpuUpstreamHallOrder.objects.create(**self._mirror_kwargs())
        # 换地址 ⇒ 建成第二行（两家上家的单号会撞，所以唯一键必须带地址）
        second = JidanpuUpstreamHallOrder.objects.create(
            **self._mirror_kwargs(upstream_base_url=BASE_URL_B)
        )
        self.assertEqual(second.upstream_base_url, BASE_URL_B)

    def test_full_clean_passes_with_only_the_required_fields(self):
        """可空 FK / 可空展示列都不进 `full_clean()` 的必填面（`blank`·`null` 语义）。"""
        JidanpuUpstreamHallOrder(**self._mirror_kwargs()).full_clean()

        field = JidanpuUpstreamHallOrder._meta.get_field("owner_binding")
        self.assertTrue(field.null)
        self.assertTrue(field.blank)
        for name in ("game", "claiming_by", "promoted_order"):
            optional = JidanpuUpstreamHallOrder._meta.get_field(name)
            self.assertTrue(optional.null, name)
            self.assertTrue(optional.blank, name)


    def test_promoted_order_is_protected_and_unique(self):
        """⭐ 评审补（ 补丁）：`promoted_order`（`OneToOneField(Order, PROTECT, null=True)`）
    的**行为**。⛔ 不断言 `field.remote_field.on_delete is PROTECT` 这类属性字符串
    ——那是「钉常量」：把 `on_delete` 改成 `CASCADE` / `SET_NULL`，属性断言照样绿。

    ⚠️ ② 那条新行的远端单号**刻意换一个**（⛔ 不沿用 `REMOTE_ORDER_ID`）：沿用的话
    `IntegrityError` 会来自 `(upstream_base_url, upstream_order_id)` 那条**表级**唯一约束
    ⇒ 与 `promoted_order` 无关，这条用例就成了**恒真**（普通 `ForeignKey` 也能过）。
"""
        order = Order.objects.create(game=self.game, order_no="20260928000000009301")
        mirror = JidanpuUpstreamHallOrder.objects.create(
            **self._mirror_kwargs(promoted_order=order)
        )

        # ① PROTECT **行为**：删被指着的我方订单 ⇒ 抛（并钉住那一行真的还在、指向没变）
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                order.delete()
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())
        mirror.refresh_from_db()
        self.assertEqual(mirror.promoted_order_id, order.pk)

        # ② OneToOne 唯一：第二行再指同一张订单 ⇒ 撞唯一约束，且没有第二行落库。
        # ⚠️ 除「抛了异常」外，还**当场辨认**这个 `IntegrityError` 出自哪条约束
        # （`promoted_order_id` 在违反的约束名里）——否则别的唯一约束替它抛异常也照样绿。
        with self.assertRaises(IntegrityError) as error:
            with transaction.atomic():
                JidanpuUpstreamHallOrder.objects.create(
                    **self._mirror_kwargs(
                        upstream_order_id="upstream-order-0009", promoted_order=order,
                    )
                )
        constraint = getattr(getattr(error.exception.__cause__, "diag", None),
                             "constraint_name", "") or ""
        self.assertIn("promoted_order_id", constraint)
        self.assertEqual(
            JidanpuUpstreamHallOrder.objects.filter(promoted_order=order).count(), 1
        )

        # ③ ⚠️ **先钉「多行 NULL 合法」**（该列 `null=True`）：不钉这条，② 的「唯一」会被读成
        # 「这张表只准有一行」；再加一张**另一张订单**的镜像行 ⇒ 唯一是**按订单**的
        # （⛔ 不是「全表只准一行非空」）。
        for upstream_order_id in ("upstream-order-0010", "upstream-order-0011"):
            null_row = JidanpuUpstreamHallOrder.objects.create(
                **self._mirror_kwargs(upstream_order_id=upstream_order_id)
            )
            self.assertIsNone(null_row.promoted_order)
        self.assertEqual(JidanpuUpstreamHallOrder.objects.filter(promoted_order=None).count(), 2)

        other_order = Order.objects.create(game=self.game, order_no="20260928000000009302")
        other_mirror = JidanpuUpstreamHallOrder.objects.create(
            **self._mirror_kwargs(upstream_order_id="upstream-order-0012",
                                  promoted_order=other_order)
        )
        self.assertEqual(other_mirror.promoted_order_id, other_order.pk)


class UpstreamTableShapeTests(TenantTestCase):
    """表名 / 列名 / 三条 `on_delete` 各档（spec §4.1–§4.3、§8.4 末尾那条补句）。"""

    def _columns(self, model):
        with connection.cursor() as cursor:
            return {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor, model._meta.db_table
                )
            }

    def test_table_and_column_names_are_the_planned_ones(self):
        self.assertEqual(JidanpuUpstreamBinding._meta.db_table, BINDING_TABLE)
        self.assertEqual(JidanpuUpstreamOrderLink._meta.db_table, LINK_TABLE)
        self.assertEqual(JidanpuUpstreamHallOrder._meta.db_table, HALL_TABLE)
        self.assertEqual(self._columns(JidanpuUpstreamBinding), BINDING_COLUMNS)
        self.assertEqual(self._columns(JidanpuUpstreamOrderLink), LINK_COLUMNS)
        self.assertEqual(self._columns(JidanpuUpstreamHallOrder), HALL_COLUMNS)

    def test_the_three_on_delete_policies_are_distinct_on_purpose(self):
        """⚠️ 三档**刻意不同**（spec §8.4 末尾的补句）：删号 ⇒ 绑定没；链路随绑定消失；
    镜像行**必须活下来**（`SET_NULL`）⇒ ⛔ 不要「统一」。"""
        self.assertIs(
            JidanpuUpstreamBinding._meta.get_field("user").remote_field.on_delete, models.CASCADE
        )
        self.assertIs(
            JidanpuUpstreamOrderLink._meta.get_field("binding").remote_field.on_delete,
            models.CASCADE,
        )
        self.assertIs(
            JidanpuUpstreamOrderLink._meta.get_field("order").remote_field.on_delete,
            models.CASCADE,
        )
        owner = JidanpuUpstreamHallOrder._meta.get_field("owner_binding").remote_field.on_delete
        self.assertIs(owner, models.SET_NULL)

    def test_deleting_the_binding_removes_links_but_keeps_the_mirror(self):
        """行为面重钉一次：`CASCADE` 的链路没了、`SET_NULL` 的镜像行还在（且主人置空）。"""
        binding = _make_binding(username="upstream-a")
        game = Game.objects.create(name="合成游戏甲")
        order = Order.objects.create(game=game, order_no="20260928000000009201")
        link = JidanpuUpstreamOrderLink.objects.create(
            binding=binding, order=order, upstream_order_id=REMOTE_ORDER_ID,
        )
        mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL_A, upstream_order_id=REMOTE_ORDER_ID,
            owner_binding=binding, first_seen_at=timezone.now(),
        )

        binding.delete()

        self.assertFalse(JidanpuUpstreamOrderLink.objects.filter(pk=link.pk).exists())
        mirror.refresh_from_db()
        self.assertIsNone(mirror.owner_binding)

    def test_mirror_game_and_claiming_user_are_protected(self):
        """游戏与「正在接单的我方打手」都是 `PROTECT`（照 `RemoteHallOrder` 同形）：
    有镜像行指着的游戏 / 人在指向被清掉之前删不掉。

    ⛔ **2026-10-08 改准（「照 `RemoteHallOrder` 同形」一句是历史原文）**：那个租户级的
    镜像表已随该线整条拆除删除 ⇒ 这句今天指**当年照它同形的来源**；⚠️ 本用例钉的
    `PROTECT` 行为（下面两条 `assertRaises`）**一个字未改**。
"""
        game = Game.objects.create(name="合成游戏甲")
        claimer = User.objects.create_user(username="upstream-claimer", password="pw-a")
        JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL_A, upstream_order_id=REMOTE_ORDER_ID,
            game=game, claiming_by=claimer, first_seen_at=timezone.now(),
        )
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                game.delete()
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                claimer.delete()

    def test_unknown_status_is_rejected_by_full_clean(self):
        """`choices` 是承重的：非法状态在 `full_clean()` 处被拒（对照：合法值不抛）。"""
        kwargs = dict(
            upstream_base_url=BASE_URL_A, upstream_order_id=REMOTE_ORDER_ID,
            first_seen_at=timezone.now(),
        )
        JidanpuUpstreamHallOrder(status="listed", **kwargs).full_clean()
        with self.assertRaises(ValidationError) as error:
            JidanpuUpstreamHallOrder(status="bogus", **kwargs).full_clean()
        self.assertIn("status", error.exception.message_dict)


# ── 官方 Open Platform API 那一组列 + 部分唯一索引（T1；spec §2.1 / §4.2 / §4.4；§13.3 的 C1/C5/C6）──
# ⚠️ 本节的判据一律打在**数据库行 / 数据库里的索引定义 / 列默认值**上（`AGENTS.md` §5-G）：
# 「迁移后既有绑定仍是登录式」这条**不能**靠「读模型默认值」证明 —— 那只证明**新行**的默认值，
# 说的是另一件事（既有行在迁移那一刻是被 `ADD COLUMN … DEFAULT` 填的）。

#: 那串随机码的形状（spec §5.1）：`secrets.token_urlsafe(16)`。
#: ⚠️ **22 是承重的**：`deploy/nginx/tuantuantong.conf` 的 location 用定长 22 的正则钉住回调路径。
CALLBACK_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22}$")

#: 部分唯一索引的名字（spec §4.4 第 2 步的 `name=`，⛔ 不从模型派生）。
CALLBACK_CODE_CONSTRAINT = "uniq_jdp_upstream_callback_code"


def _make_openapi_binding(*, username="upstream-openapi", base_url=BASE_URL_B, account="acc-openapi",
                          code=None):
    """造一条**官方 API 方式**的绑定。

    ⚠️ `code` 显式给了就原样用（C5 要构造「两行同码」）；不给则由模型自己生成（C5 的自动生成那一半）。
"""
    user = User.objects.create_user(username=username, password="pw-a")
    return JidanpuUpstreamBinding.objects.create(
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


def _adds_mode_column_operation(migration):
    """`migration` 里那条「给 `JidanpuUpstreamBinding` 加 `mode` 列」的 `AddField`（没有 ⇒ `None`）。

    ⭐ 语义标识的唯一出处（`_target_key()` 与「找到的就是加 `mode` 那一列的那条」那条判据都读它）：
    认的是 `AddField` 的 `model_name` + `name` 两个**语义**字段，
    ⛔ 不是文件名、⛔ 不是序号前缀、⛔ 不是它在图里的位置（合并后同前缀两条的坑见 `_target_key()`）。
"""
    for operation in migration.operations:
        if (type(operation).__name__ == "AddField"
                and operation.model_name == "jidanpuupstreambinding"
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


#: 造绑定行时写的列（原生 SQL）—— ⚠️ **刻意不含 `mode` / `callback_code` 等 0057 新列**：
#: 这份清单就是「迁移前那种行」的列集，给 `UpstreamOfficialApiMigrationTests` 在
#: **结构已退到 0057 之前**时造夹具用（那时表上真的没有这些列）。
#: ⚠️ **实测两条（都踩过）**：
#: ① `account_name` / `hall_cursor` / `last_error` 是 `NOT NULL` 且**列上没有数据库默认值**
#: ⇒ 漏了它们直接 `NotNullViolation`；
#: ② **新加的列也没有数据库默认值**（Django 在 `ADD COLUMN … DEFAULT` 回填**之后**会把那个
#: 临时默认值 `DROP` 掉，读码：`django/db/backends/base/schema.py` 的 `add_field` 末段；
#: **实测**：本表所有列迁移后 `column_default` 全是 `None`）⇒ 在**已升级**的表上省略
#: `callback_code` 一样是 `NotNullViolation`。
#: ⇒ 结论：**在已升级的表上没法用「省略新列」来假造迁移前的行**（会撞 `NOT NULL`）。
#: ⇒ 真形态只能靠「把结构退到 0057 之前再插」——见
#: `UpstreamOfficialApiMigrationTests`（走 `MigrationExecutor` 反向再正向）。
LEGACY_COLUMNS = (
    ("jidanpu_base_url", "%s"),
    ("account", "%s"),
    ("account_name", "''"),
    ("password", "%s"),
    ("enabled", "false"),
    ("hall_price_percent", "100"),
    ("hall_poll_seconds", "300"),
    ("my_orders_poll_seconds", "60"),
    ("hall_cursor", "''"),
    ("last_error", "''"),
    ("created_at", "now()"),
    ("updated_at", "now()"),
    ("user_id", "%s"),
)

#: **已升级**的表上要补写的新列（都是 `NOT NULL` 且**没有**列默认值 ⇒ 原生 SQL 必须显式给值）。
#: ⚠️ 只在「结构已经是 0057」的用例里用（部分唯一索引那条），⛔ 不用于迁移夹具。
POST_0057_COLUMNS = (
    ("mode", "'password'"),
    ("callback_code", "''"),
    ("client_id", "''"),
    ("client_secret", "''"),
    ("callback_count", "0"),
    ("reconcile_seconds", "600"),
    #: ⭐ 0091：`BigIntegerField(default=0)` ⇒ 迁移后列默认值被 DROP
    #: （Django 的 `ADD COLUMN … DEFAULT` 只用来回填存量行）⇒ 原生 INSERT 必须显式写。
    ("callback_file_offset", "0"),
    #: ⭐ 0123：人填的「上家系统名称」（`CharField(default="")`）⇒ 同上，
    #: 迁移后列默认值被 DROP ⇒ 已升级的表上原生 INSERT 必须显式写空串。
    ("name", "''"),
)
_LEGACY_USERNAME_SEQ = iter(range(1, 1000))


def _insert_binding_row(*, user_id, base_url, account, password="pw-1", with_new_columns=True):
    """用**原生 SQL** 插一行绑定（⛔ 不走 ORM：故意绕开模型层，只碰列）。

    ⚠️ `with_new_columns`：**已升级**的表要写 `POST_0057_COLUMNS` 那几列（它们 `NOT NULL`、
    没有列默认值），**退到 0057 之前**的表则⛔ 不能写（那时列还不存在）。
"""
    columns = list(LEGACY_COLUMNS)
    if with_new_columns:
        columns = columns[:10] + list(POST_0057_COLUMNS) + columns[10:]
    names = [name for name, _placeholder in columns]
    placeholders = [placeholder for _name, placeholder in columns]
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {BINDING_TABLE} ({', '.join(names)}) "
            f"VALUES ({', '.join(placeholders)}) RETURNING id",
            [base_url, account, password, user_id],
        )
        return cursor.fetchone()[0]


def _insert_legacy_binding(*, base_url, account, password="pw-1"):
    """造一行「多行 `mode="password"` + 空码」形态的行（供**部分唯一索引**那条用例）。

    ⚠️ 它**不是**「迁移前那种行」的判据（那要靠退结构，见 `LEGACY_COLUMNS` 上方的说明）。
"""
    user = User.objects.create_user(
        username=f"legacy-upstream-{next(_LEGACY_USERNAME_SEQ)}", password="pw-a",
    )
    return _insert_binding_row(
        user_id=user.pk, base_url=base_url, account=account, password=password,
        with_new_columns=True,
    )


class UpstreamOfficialApiModeTests(TenantTestCase):
    """`mode` 这一列 = 「用哪套凭证」的**唯一**判据（spec §2.1）。"""

    def test_mode_defaults_to_password_in_the_model(self):
        """**C1**：`mode` 的默认值是 `"password"`（⛔ 不是 `openapi`）。

    ⚠️ 期望值写字面量 `"password"`，⛔ 不写 `JidanpuUpstreamBinding.Mode.PASSWORD`
    ——那是自指断言，常量被改坏时两边一起变、永远不会红（E-0024）。
"""
        self.assertEqual(JidanpuUpstreamBinding._meta.get_field("mode").default, "password")
        self.assertEqual(
            [value for value, _label in JidanpuUpstreamBinding._meta.get_field("mode").choices],
            ["password", "openapi"],
        )
        binding = _make_binding()
        self.assertEqual(binding.mode, "password")
        binding.refresh_from_db()
        self.assertEqual(binding.mode, "password")

    def test_mode_and_client_columns_match_the_spec_shape(self):
        """九列的**可空性 / 长度 / 默认值**逐条钉死（spec §4.2 的表）。

    ⚠️ 这些是「新行」的口径：`client_id` / `client_secret` / `callback_code` 是
    `blank=True, default=""`（**NOT NULL 的空串**，⛔ 不是 NULL）——「没填」与「填了空」
    在库里是同一个值；⚠️ 三个时刻列才是 `null=True`。
"""
        field = JidanpuUpstreamBinding._meta.get_field
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
            # （`assertIsNone(column.default)` 对这三个可空列**必红**——踩过一次）。
            self.assertIs(column.default, models.NOT_PROVIDED, name)
            self.assertTrue(column.null, name)
            self.assertTrue(column.blank, name)
        self.assertEqual(field("callback_count").default, 0)
        self.assertEqual(field("reconcile_seconds").default, 600)

        binding = _make_binding()
        self.assertEqual(binding.client_id, "")
        self.assertEqual(binding.client_secret, "")
        self.assertEqual(binding.callback_code, "")
        self.assertEqual(binding.callback_count, 0)
        self.assertEqual(binding.reconcile_seconds, 600)
        for name in ("callback_code_at", "callback_watermark_at", "callback_last_at"):
            self.assertIsNone(getattr(binding, name), name)

    def test_openapi_mode_generates_exactly_one_code_and_stamps_it(self):
        """**C5（自动生成那一半）**：`mode="openapi"` ⇒ 保存时**自动**生成码 + 时间戳。

    ⚠️ 对照（同一用例里做）：①式行**一个字节都不写**（码仍为空串）——⛔ 别把生成写成
    「无条件生成」：那会让每一行登录式绑定都凭空多一串用不到的码（而且它们都 `mode="password"`、
    看着无害，实际下次判「谁有码」的人会被误导）。
"""
        binding = _make_openapi_binding()
        self.assertTrue(CALLBACK_CODE_PATTERN.match(binding.callback_code),
                        f"码的形状不对：{binding.callback_code!r}")
        self.assertIsNotNone(binding.callback_code_at)
        # 落库的不是内存里那份（防止「只 set 了属性、没进 update_fields / 没进 INSERT」）
        binding.refresh_from_db()
        self.assertTrue(CALLBACK_CODE_PATTERN.match(binding.callback_code))
        self.assertIsNotNone(binding.callback_code_at)
        self.assertEqual(binding.callback_code_at.tzinfo is not None, True)

        password_binding = _make_binding(username="upstream-plain")
        self.assertEqual(password_binding.callback_code, "")
        self.assertIsNone(password_binding.callback_code_at)

    def test_saving_an_openapi_row_again_keeps_the_same_code(self):
        """再存一次**不换码**（换码 = 用户此前填进鸡蛋铺的那个地址静默失效，只有人能决定）。"""
        binding = _make_openapi_binding()
        code, stamped_at = binding.callback_code, binding.callback_code_at
        binding.account_name = "改个名字"
        binding.save()
        binding.refresh_from_db()
        self.assertEqual(binding.callback_code, code)
        self.assertEqual(binding.callback_code_at, stamped_at)

    def test_generation_survives_an_update_fields_write(self):
        """⭐ 走 `update_fields` 的写入点（`jidanpu_upstream_views` 的 PUT 那一支）也要把码写进去。

    ⚠️ 这条盯的是一个**静默**形态：只 `set` 属性而 `update_fields` 里没有那两列 ⇒
    码看起来生成了（内存里在）、库里是空的 ⇒ 页面把地址拼出来是**半截**的、回调 404。

    ⚠️ 形态照 `jidanpu_upstream_views` 的 PUT 那一支：**先有行**（`pending_instance()` 拿到的
    是已存在的实例），再 `save(update_fields=...)`。⛔ 不能对没落库的实例直接
    `save(update_fields=...)`——那会 `ValueError: Cannot force an update in save() with no
    primary key`（踩过一次），测的就成了 Django 的行为而不是我们的。
"""
        existing = _make_binding(username="upstream-updatefields")   # 先以①式落一行
        existing.mode = "openapi"
        existing.client_id = "cid-uf"
        existing.save(update_fields=["jidanpu_base_url", "account", "password", "mode",
                                     "client_id", "updated_at"])
        existing.refresh_from_db()
        self.assertTrue(CALLBACK_CODE_PATTERN.match(existing.callback_code),
                        f"码没落库：{existing.callback_code!r}")
        self.assertIsNotNone(existing.callback_code_at)

    def test_code_format_holds_for_a_thousand_draws(self):
        """**C6**：码的形状 22 字符 + 只含 `A-Za-z0-9-_`（跑 1000 次都不出格）。

    ⚠️ **取样必须经过模型**（`save()` 落库后 `refresh_from_db()` 读回来），⛔ 不是直接调
    `secrets.token_urlsafe`。原版是后者，独立评审指出：**把模型的生成方式换成
    `uuid4().hex` 它照样绿** ⇒ 那条判据其实**没经过被测对象**（`AGENTS.md` §5-G 的
    E-0024 家族：断言要打在真实出口上；恒真的判据等于没判）。
    ⚠️ 每轮先把行上的码清空再 `save()` ⇒ 走的正是**模型**那条生成路径
    （`_ensure_callback_code`）；`refresh_from_db()` 保证抽到的是**落库后**的值
    （"只 set 了属性、没进 `update_fields`" 这种静默形态也会红）。
    弄红它：把 `_ensure_callback_code` 里那句 `secrets.token_urlsafe(...)` 换成
    `uuid4().hex`（32 位 hex）⇒ 长度与字符集两条断言都红（本任务**实测过**，读数在报告里）。
"""
        binding = _make_openapi_binding(username="code-shape-sampler")
        seen = set()
        for _ in range(1000):
            binding.callback_code = ""
            binding.callback_code_at = None
            binding.save()
            binding.refresh_from_db()
            code = binding.callback_code
            self.assertEqual(len(code), 22, code)
            self.assertTrue(CALLBACK_CODE_PATTERN.match(code), code)
            seen.add(code)
        # 1000 次里不该出现重复（128 位熵；真撞了说明生成器不是这套）
        self.assertEqual(len(seen), 1000)


class UpstreamCallbackCodeConstraintTests(TenantTestCase):
    """⭐ **部分**唯一索引（spec §4.2 / §4.4 第 2 步）：`mode="openapi"` 的行之间码必须唯一。

    ⚠️ 「部分」两个字是承重的、不是修饰：条件写成 `Q(mode="openapi")` 之后，
    **①式行全部 `callback_code=""` 这件事才是合法的**（PostgreSQL 里 `''` 不是 NULL）。
    ⇒ 本节两条**方向相反**的用例缺一不可：一条证「②式重复 ⇒ 拒」，一条证「①式多行空串 ⇒ 放行」。
"""

    def test_the_index_is_really_a_partial_one_on_the_database(self):
        """索引**真的**在数据库里，而且真的带 `WHERE mode='openapi'`。

    ⚠️ 只断言「模型里写了那条 `UniqueConstraint`」证明不了索引建上了
    （约束类变异必须同时改模型与迁移，`AGENTS.md` §5-G）；这里读的是 `pg_indexes.indexdef`
    —— 数据库自己怎么描述这条索引。
"""
        indexes = _index_definitions(JidanpuUpstreamBinding._meta.db_table)
        self.assertIn(CALLBACK_CODE_CONSTRAINT, indexes)
        definition = indexes[CALLBACK_CODE_CONSTRAINT]
        self.assertIn("UNIQUE", definition.upper())
        self.assertIn("callback_code", definition)
        self.assertIn("WHERE", definition.upper())
        self.assertIn("openapi", definition)

        constraints = connection.introspection.get_constraints(
            connection.cursor(), JidanpuUpstreamBinding._meta.db_table,
        )
        self.assertIn(CALLBACK_CODE_CONSTRAINT, constraints)

    def test_two_openapi_rows_may_not_share_a_code(self):
        """**C5（唯一那一半）**：两行 `mode="openapi"` 同码 ⇒ `IntegrityError`。

    ⚠️ 除「抛了」以外，还**当场辨认**这个 `IntegrityError` 出自**哪条**约束
    （约束名必须在报错里）——否则 `uniq_jdp_upstream_account` 那条表级唯一约束
    替它抛异常也照样绿（那正是「钉了别的约束」的形态）。
"""
        code = "Zx9_callback_code_AA"          # 22 字符、合成值
        _make_openapi_binding(username="openapi-a", base_url=BASE_URL_A, account="acc-a", code=code)
        with self.assertRaises(IntegrityError) as error:
            with transaction.atomic():
                _make_openapi_binding(username="openapi-b", base_url=BASE_URL_B, account="acc-b",
                                      code=code)
        constraint = getattr(getattr(error.exception.__cause__, "diag", None),
                             "constraint_name", "") or ""
        self.assertEqual(constraint, CALLBACK_CODE_CONSTRAINT)
        self.assertEqual(
            JidanpuUpstreamBinding.objects.filter(callback_code=code).count(), 1,
        )

    def test_password_rows_all_keep_an_empty_code(self):
        """⭐ **「部分」为什么必须有**：多行 `mode="password"` 全部 `callback_code=""` ⇒ **放行**。

    ⚠️ 这两行走**原生 SQL**（`_insert_legacy_binding`）：先建后写都不经过模型层，
    测的只有数据库那条索引判据本身。
    ⚠️ 弄红它：把 `condition=Q(mode="openapi")` 去掉（约束类变异要**同一次**改模型与迁移，
    见 `AGENTS.md` §5-G）⇒ 第二行 `INSERT` 直接撞唯一约束 ⇒ 本用例红。
    ⚠️ 反向对照在隔壁 `test_two_openapi_rows_may_not_share_a_code`：②式同码必须拒。
    两条缺一不可——只证「重复会拒」证明不了这条索引是**部分**的。
"""
        row_ids = [
            _insert_legacy_binding(base_url=f"https://legacy-{index}.test.invalid",
                                   account=f"legacy-acc-{index}")
            for index in (1, 2)
        ]

        rows = JidanpuUpstreamBinding.objects.filter(pk__in=row_ids).order_by("pk")
        self.assertEqual(rows.count(), 2)
        for row in rows:
            self.assertEqual(row.mode, "password")
            self.assertEqual(row.callback_code, "")
            self.assertIsNone(row.callback_code_at)


def _business_leaf_nodes():
    """迁移图上 `business` 的**叶子**（纯元组；⛔ 不写死 `0058_*` 之类）。"""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    return [tuple(node) for node in loader.graph.leaf_nodes() if node[0] == "business"]


def _applied_migrations():
    """迁移记账表里**当前**已应用的键集合（当场读表，⛔ 不是图上的名字）。"""
    return set(MigrationRecorder(connection).applied_migrations())


def _migrate_forward_to_leaves():
    """把**当前 schema** 前放到迁移图上的**全部叶子**（⛔ 不写死任何迁移名）。

    ⭐ 为什么不能只前放到「本线那条」：合并 `main` 之后本线那条**不是叶子**
    （实测：`business` 里同时有本线的 `0057_…callback_code_and_more` 与 main 的
    `0057_feedback_feedbackimage`，叶子是 `0058_merge_20261009_0024`）⇒ 只前放到本线那条会把库
    停在一个「**少别人那张表**」的状态上，而**同一进程里后面的用例**就在那张库上跑
    （症状看起来与本任务无关）。

    ⚠️ **实测**：迁移的 DDL 与记账都是**提交**的（`TenantTestCase` 只回滚用例自己的行数据）
    ⇒ 少前放一次**真的**会漏到下一个用例的 `setUp`，不是理论担忧。

    ⚠️ 传给 `migrate()` 一律 `tuple(node)`：`leaf_nodes()` 给的是 `Node` 对象，
    而 `graph.nodes` 里装的是**纯元组**（同 `_state_before_nodes()` 的踩坑）。
"""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    MigrationExecutor(connection).migrate([tuple(node) for node in loader.graph.leaf_nodes()])


class UpstreamOfficialApiMigrationTests(TenantTestCase):
    """⭐ 迁移 `0057_*` **真的从头跑一遍**：既有行迁移后依然按**登录式**读（spec §4.4）。

    ## 为什么不能只断言「新行默认 password」

    存量行**不是**被 Python 默认值填的，是被 `ADD COLUMN … DEFAULT 'password'` 那条 DDL 回填的
    （Django 的 `add_field`：那个列默认值只为回填存在，随后 `DROP DEFAULT`；**实测**：
    本表所有列迁移完之后 `column_default` 全是 `None`）。⇒「新行的默认值」与
    「既有行被填成什么」是**两件事**，前者全绿也证明不了后者。

    ## 口径：把 `0057` 退掉 → 造迁移前那种行 → 正向重放（照 `test_operation_log_migration.py` 的配方）

    ⛔ 不用「只退**记账**、结构不动」那条近路（那是 `test_operation_log_migration.py` 的形态，
    因为它的迁移只改**行**）：本迁移加的是**列**，列还在时用历史模型 `INSERT` 会被
    `NOT NULL` 直接拒（`callback_code` 等新列 `NOT NULL` 且**没有**列默认值）——
    **实测踩过**。⇒ 本用例走完整的**结构**回退再前放：

    1. `MigrationExecutor.migrate(<0056>)` ⇒ `0057` 反向执行（删那 9 列 + 删索引）；
    2. 用 `0056` 那一版**历史模型**造行（那时**没有** `mode` 这一列，正是迁移前的真实形态）；
    3. `MigrationExecutor.migrate(<0057>)` ⇒ `AddField` 对**已存在**的这些行执行
    `ADD COLUMN … DEFAULT 'password'`（PostgreSQL 会**回填既有行**）—— 这就是真迁移走的那条路；
    4. 断言这些行读回来仍是 `password`、且①式那条取数路径没变。

    ⚠️ `0057` 的反向会**删列**（spec §4.4 的「回滚代价」明写这条：丢的是全部官方 API 凭证与
    回调码）。本用例只在**测试库**里用一次、且每一轮都由 `TenantTestCase` 的事务回滚收口，
    ⛔ 不是给生产写一个会删数据的动作。
    ⚠️ `TenantTestCase` 类级建 schema / 用例级事务回滚：DDL 在这里能回滚（PostgreSQL 事务 DDL）。
"""

    #: `0057` 之前那一版的历史模型（缓存，按进程只算一次）。
    _STATE_BEFORE = None

    def setUp(self):
        super().setUp()
        #: ⭐ **前置检查（安全网，不是判据本体）**：迁移的 DDL / 记账是**提交**的（只有用例自己那些行
        #: 数据被事务回滚）⇒ 上一条用例若没把结构前放回叶子，**本用例就在这里**看到缺列 / 缺表。
        #: **实测**：改造前 `business_feedback`（另一条 `0057` 的叶子产物）在每个用例的 `setUp` 里都在，
        #: 说明它在上一条用例前放之后确实留了下来；而只前放到「本线那条」时它就会缺。
        #: ⚠️ **实测的边界**（⛔ 别把它当承重判据读）：单跑
        #: `test_all_business_leaves_are_applied_after_this_class_runs` 并把 `tearDown` 的前放退化成
        #: `migrate([self._target_key()])` 时，**本条单独不会红**（用例体自己已经前放过了）
        #: —— 它抓的是「**某条用例把烂状态漏给了下一条**」，而那正好是本类的真实执行形态。
        #: ⇒ 与 `test_all_business_leaves_are_applied_after_this_class_runs` **一对**用，⛔ 别只留一个。
        self.assertEqual(
            sorted(node for node in _business_leaf_nodes() if node not in _applied_migrations()),
            [],
            "上一条用例收尾时没把结构前放回叶子 ⇒ 本用例跑在缺表/缺列的库上",
        )
        key = self._target_key()
        if key is None:
            #: ⚠️ 先立标志再 skip：`skipTest` 是抛异常退出的，写在它后面的行不会执行。
            self._skipped_no_target = True
            self.skipTest("迁移图上按语义找不到「给 JidanpuUpstreamBinding 加 mode 列」那条迁移"
                          "（被 squash / 改名 / 有两条？）⇒ 本判据另行处理")
        MigrationExecutor(connection).migrate(list(self._state_before_nodes()))

    def tearDown(self):
        # ⚠️ 还原进 `tearDown`（`try/finally` 语义）：用例断言失败时也要把结构前放回去，
        # 否则同一进程里后面的用例会在「少 9 列 / 少别人那张表」的库上跑
        # （症状看起来像与本任务无关的乱红）。
        # ⚠️ **无条件**前放（⛔ 不用 `self._rewound` 那种标志）：用例体自己前放过一次也无妨——
        # `migrate()` 对已应用的迁移是 no-op，而漏跑一次的代价是**下一个用例红**。
        if not getattr(self, "_skipped_no_target", False):
            _migrate_forward_to_leaves()
        super().tearDown()

    def _state_before_nodes(self):
        """本线那条迁移的父节点（= 迁移前的状态）。

    ⚠️ **故意不跟着 `_migrate_forward_to_leaves()` 改**：它要的正是「我这条**之前**」的状态，
    ⛔ 不是叶子。

    ⚠️ 必须 `tuple(parent)`：`graph.node_map[...].parents` 里装的是 `Node` 对象，
    而 `graph.nodes` 里装的是**纯元组** ⇒ 直接把 `Node` 交给 `project_state` / `migrate`
    会在 `forwards_plan` 的 `target not in self.nodes` 处炸
    （**实测**：`TypeError: unhashable type: 'list'`）。
"""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        return [tuple(parent) for parent in loader.graph.node_map[self._target_key()].parents]

    def _target_key(self):
        """本任务那条迁移在图里的**键** `(app_label, name)`（当场**按语义**找，⛔ 不按序号前缀）。

    ⭐ 认它的唯一依据是它自己的 `operations`：那条 `AddField(model_name="jidanpuupstreambinding",
    `name="mode")`。⛔ 不硬编码完整文件名、⛔ 不硬编码日期后缀、⛔ 不用 `startswith("0057_")`。

    序号前缀合并后不再唯一。按 `disk_migrations` 的枚举顺序取，每个进程可能取到另一条，
    表上就没有 `mode` 列。裸标识符 `mode` 会被 PostgreSQL 当成聚合函数，报
    `WITHIN GROUP is required for ordered-set aggregate mode`——意思是「当时没有这一列」。

    被 squash / 改名 ⇒ `None` ⇒ 用例 skip（⛔ 不假绿）。
"""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        candidates = [
            key for key, migration in loader.disk_migrations.items()
            if key[0] == "business" and _adds_mode_column_operation(migration) is not None
        ]
        #: ⚠️ 恰好一条才认它：两条（各写了一次 `AddField`）说明结构变了，
        #: 那时「哪条是本文要重放的那条」没有答案 ⇒ 宁可 skip。
        return candidates[0] if len(candidates) == 1 else None

    def _state_before(self):
        """`0057` 之前那一版的历史模型（含 `0056` 时的 `JidanpuUpstreamBinding`）。"""
        if UpstreamOfficialApiMigrationTests._STATE_BEFORE is None:
            loader = MigrationLoader(None, ignore_no_migrations=True)
            UpstreamOfficialApiMigrationTests._STATE_BEFORE = loader.project_state(
                self._state_before_nodes()
            )
        return UpstreamOfficialApiMigrationTests._STATE_BEFORE

    def _legacy_rows(self, base_urls):
        """在**结构已退到 `0057` 之前**的表上（连带 `auth` 也退到 `0001_initial`）造「迁移前那种行」。

    ⚠️ 两条都**实测**踩过，缺一条就假红：
    ① 反向迁移会把 **`auth` 这个依赖 app 一起退掉**（`0057` 依赖 `auth.0001_initial`）
    ⇒ 那时 `User.objects.create_user(...)` 报
    `NotNullViolation: null value in column "last_login"` ⇒ **User 也必须用原生 SQL 插**；
    ② 表上此刻**没有** `mode` / `callback_code` 等新列 ⇒ 行只写 `LEGACY_COLUMNS`。
"""
        historical = self._state_before().apps.get_model("business", "JidanpuUpstreamBinding")
        self.assertNotIn(
            "mode", [field.name for field in historical._meta.get_fields()],
            "历史模型里已经有 mode ⇒ 这不是 0057 之前的状态，本用例无效",
        )
        row_ids = []
        for index, base_url in enumerate(base_urls, start=1):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s AND column_name = 'mode'",
                    [connection.schema_name, BINDING_TABLE],
                )
                self.assertIsNone(cursor.fetchone(), "结构没退干净：mode 列还在（那本用例无效）")
                cursor.execute(
                    "INSERT INTO auth_user (password, is_superuser, username, first_name, "
                    " last_name, email, is_staff, is_active, date_joined, last_login) "
                    "VALUES ('', false, %s, '', '', '', false, true, now(), now()) RETURNING id",
                    [f"hist-upstream-{index}"],
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
        self.assertEqual(_migration_add_field_default("jidanpuupstreambinding", "mode"), "password")

    def test_existing_rows_are_still_password_mode_after_the_migration(self):
        """⭐ **本任务最重要的判据**：重放迁移之后，既有的“登录式”行**依然是登录式**。

    ⚠️ 弄红它：把 `mode` 的 `default` 换成 `"openapi"`（模型 + 迁移两处一起改，
    照 `AGENTS.md` §5-G 的约束类纪律：只改一处读数就不指向任何单条改动）
    ⇒ 回填把这两行填成 `openapi` ⇒ 第一条断言红。
    ⚠️ 顺带钉住两件事：①式行**不需要**回调码；迁移里那条**部分**唯一索引对多行空码**放行**
    （它要是被写成全表唯一，前放这一步的第二行回填就会以 `IntegrityError` 炸在这里）。
"""
        rows = self._legacy_rows([BASE_URL_A, BASE_URL_B])
        self.assertEqual(len(rows), 2)

        #: ⚠️ **前放回到全部叶子**（⛔ 不是 `migrate([self._target_key()])`）：本线之后已不是叶子
        #: ⇒ 只前放到它会把库停在「少别人那张表」的状态，而**同一进程里后面的用例**就在那张库上跑。
        _migrate_forward_to_leaves()

        #: ⭐ 原判据的另一半（与 `_legacy_rows` 里那条 `mode` 列**不在**的断言对称）：
        #: 「退到本线那条之前 ⇒ `mode` 列真的不在」↔「前放 ⇒ `mode` 列真的在」。
        #: ⚠️ 少这一条的话，「前放到底跑没跑」只剩行读数间接兜着。
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s AND column_name = 'mode'",
                [connection.schema_name, BINDING_TABLE],
            )
            self.assertEqual(cursor.fetchone(), ("mode",), "前放之后 mode 列仍然不在 ⇒ 迁移没被应用")

        for index, row_id in enumerate(rows, start=1):
            current = JidanpuUpstreamBinding.objects.get(pk=row_id)
            self.assertEqual(current.mode, "password", f"第 {index} 行被迁移改成了别的对接方式")
            self.assertEqual(current.client_id, "")
            self.assertEqual(current.client_secret, "")
            self.assertEqual(current.callback_code, "")
            self.assertIsNone(current.callback_code_at)
            self.assertEqual(current.callback_count, 0)
            self.assertEqual(current.reconcile_seconds, 600)
            # ⚠️ 承重面：登录式那条路读的还是 `password`（⛔ 没被新列挤掉）
            expected_base_url = BASE_URL_A if index == 1 else BASE_URL_B
            self.assertEqual(
                jidanpu.credentials_of(current),
                (expected_base_url, f"hist-acc-{index}", "pw-1"),
            )

    def test_the_target_key_is_the_migration_that_adds_the_mode_column(self):
        """⭐ **防这次复发**：`_target_key()` 找到的那条**确实就是**加 `mode` 那一列的那条。

    ## 为什么必须单独钉这一条

    `_target_key()` 原来按**序号前缀** `startswith("0057_")` 找。合并 `main` 后 `business` 里有
    **两条** `0057_*`（本线的 `…callback_code_and_more` 与 main 的 `0057_feedback_feedbackimage`）
    ⇒ `next(...)` 取到哪条取决于 `disk_migrations` 的**枚举顺序**，**每个进程都不一样**
    （**实测**：同一份代码连跑 4 次独立进程，1 次取到 feedback、3 次取到本线那条
    ⇒ 这也是「合并前绿、合并后红」看起来像突然坏掉的原因：它**本来就是飘的**）。
    取到 feedback 那次 ⇒ 前放时本线那条**压根没应用** ⇒ 表上没有 `mode` 列 ⇒ 读它时
    PostgreSQL 把裸标识符当聚合函数解析，报
    `WrongObjectType: WITHIN GROUP is required for ordered-set aggregate mode`
    （**这句报错的真实含义是「这张表当时没有 `mode` 列」**，⛔ 不是聚合函数用错了）。

    ⚠️ 上面那两条行读数用例**盖不住**这个缺陷：它们断言的是「本线那条被应用之后」的行为，
    而当时的失效形态是「**本线那条没被选中**」——那一步没有判据（本条补的就是它）。

    ## 弄红它（两条实测读数）

    - **M1b（确定性）**：把语义查找换成「按名字直接取 `0057_feedback_feedbackimage`」⇒ **本条红**。
    - **M1a（复刻老写法）**：换回 `next(... startswith("0057_") ...)` ⇒ **本条时红时绿**
    （实测 6 次：3 绿 3 红）——⚠️ 这正是「靠枚举顺序的判据」不可信的样子：
    它绿不代表它对，只代表这次抽到了对的顺序。

    ⚠️ 与 `test_the_migration_declares_password_as_the_default` 的分工：那条只读**迁移文件**里的默认值
    （磁盘上的文本，**不碰图**）⇒ `_target_key()` 取错了它照样绿 —— 这正是本条存在的理由。
"""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        key = self._target_key()
        self.assertIsNotNone(key, "按语义找不到加 mode 列那条迁移 ⇒ 本判据无效（照 setUp 口径应 skip）")

        #: ⭐ **恰好集合**：全 `business` 里只有一条迁移声明了这条 `AddField`
        #: ⇒ `_target_key()` 必须与它相同（两条就自相矛盾，上面 `_target_key()` 会回 `None`）。
        declaring = sorted(
            key for key, migration in loader.disk_migrations.items()
            if key[0] == "business" and _adds_mode_column_operation(migration) is not None
        )
        self.assertEqual(
            [key], declaring,
            "`_target_key()` 取到的那条不是「声明了 AddField(mode)」的那条（序号前缀当标识的老毛病）",
        )

        operation = _adds_mode_column_operation(loader.disk_migrations[key])
        self.assertEqual(operation.model_name, "jidanpuupstreambinding")
        self.assertEqual(operation.name, "mode")
        self.assertEqual(operation.field.default, "password")

    def test_all_business_leaves_are_applied_after_this_class_runs(self):
        """⭐ **前放要回到叶子**：本类跑完之后 `business` 的**所有叶子都已在库上**。

    ## 为什么必须单独钉（**实测**：这不是理论担忧）

    本类的 `setUp`/`tearDown` 会把**结构**退回去再前放。合并 `main` 之后本线那条**不是叶子**
    （实测 `business` 的叶子是 `0058_merge_20261009_0024`）⇒ 只前放到「本线那条」会把库停在一个
    **少别人那张表**的状态上。⚠️ 而**迁移的 DDL 与记账都是提交的**
    （`TenantTestCase` 只回滚用例自己的行数据）⇒ 那个状态**真的**漏到同一进程里后面的用例。
    实测证据：改造前每个用例的 `setUp` 里 `business_feedback`（另一条 `0057` 的叶子产物）都在。
    ⇒ 本判据就是防止「少前放」再次发生的那条网（`setUp` 里还有一条同口径的前置检查）。

    ## 判据打在**库的终态**上（⛔ 不是只读图上的名字）

    两条独立读数，缺一条都可能是假绿：
    ① **记账表**：`business` 的每个叶子都已在 `django_migrations` 里；
    ② **叶子产物的实际列 / 表**：`mode` 列（本线那条迁移加的）＋ `business_feedback` 表
    （**另一条** `0057` 的叶子产物）。

    ⚠️ **实测为什么 ② 必须存在**：只留 ① 时，把**用例体**里那次前放退化成
    `migrate([self._target_key()])` 本条**仍然绿**——记账表那两行是上一条用例收尾时**提交**下来的，
    而 `django_migrations` 的记录没有实体产物可查（`0058_merge` 是纯合并、什么都不建）。
    ② 打的是**真的列 / 真的表**，前放不到位就红。

    ⚠️ **弄红它的两条实测读数**：
    - 把**用例体**那次前放换回 `MigrationExecutor(connection).migrate([self._target_key()])`
    ⇒ `0058` 还挂在未应用集合里、`business_feedback` 也不在 ⇒ **本条红**（① 先红）；
    - 把 **`tearDown`** 那次前放换回 `migrate([self._target_key()])` ⇒ ⚠️ **本条单跑仍绿**
    （用例体已经前放过了，`migrate` 幂等）—— 那条变异由**下一个用例的 `setUp`** 兜住
    ⇒ `setUp` 里那条前置检查与本条是**一对**，⛔ 别只留一个。
    ⛔ 不写死 `0058_*` 之类：① 用的是图上的叶子集合，将来加迁移这里仍然指得对。
"""
        leaves = _business_leaf_nodes()
        #: ⚠️ 非空是**前提**（空集合会让下面那条断言恒真 ⇒ 假绿）。
        self.assertTrue(leaves, "`business` 在图上没有叶子 ⇒ 本判据没意义")

        #: 用例体自己前放一次（本用例走的就是「退回去再前放」那条路；`migrate` 幂等）。
        _migrate_forward_to_leaves()

        applied = _applied_migrations()
        self.assertEqual(
            sorted(node for node in leaves if node not in applied), [],
            "本类跑完之后 `business` 有叶子没被应用 ⇒ 同一进程里后面的用例跑在缺表的库上",
        )

        #: ② 叶子产物的**实际**列/表 —— 记账表说「应用了」而结构没到位（或反之）都要在这里红。
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s AND column_name = 'mode'",
                [connection.schema_name, BINDING_TABLE],
            )
            self.assertEqual(cursor.fetchone(), ("mode",),
                             "`mode` 列不在 ⇒ 加它的那条迁移没被应用（`_target_key()` 又取错了？）")
            cursor.execute("SELECT to_regclass(%s)", [f"{connection.schema_name}.business_feedback"])
            self.assertIsNotNone(
                cursor.fetchone()[0],
                "`business_feedback` 不在 ⇒ 另一条 `0057` 没被应用（前放只到「本线那条」、没回叶子）",
            )
