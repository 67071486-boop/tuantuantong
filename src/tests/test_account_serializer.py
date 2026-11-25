"""写路径序列化器（E-0010：请求来源的值进类型化的列之前一律过序列化器）。"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from apps.accounts.models import StaffProfile
from apps.accounts.payloads import account_payload
from apps.accounts.serializers import (
    CREATE_REQUIRED_FIELDS,
    AccountBatchStatusSerializer,
    AccountFilterSerializer,
    AccountWriteSerializer,
)
from apps.business.models import Game
from apps.core.error_codes import ErrorCode
from apps.platform.provisioning import seed_tenant
from apps.rbac.models import Role

User = get_user_model()

#: `grab_limit_dl` / `grab_limit_pw` 所在列的实测上界：模型字段是 `PositiveIntegerField`，
#: `db_type(connection) == "integer"`（PostgreSQL int4），字段自带的
#: `MaxValueValidator.limit_value == 2147483647`。这里**刻意写成字面量**而不是 import
#: 实现里的常量——测试要独立陈述列的边界，否则实现把常量改小/改大，两边一起动，测试就没判别力了。
INT4_MAX = 2147483647


class _WriteProbeView(APIView):
    """只给测试用的写端点：把序列化器的拒绝走一遍**项目真实的** DRF 异常管线。

    账号线的写视图要到 Task 4/6 才接线；在这之前，「这类请求会不会 500」只能靠一个
    真实 `APIView`（`dispatch` → `handle_exception` → `EXCEPTION_HANDLER` → 统一信封）来验。
    直接调 `envelope_exception_handler()` 是中间层，绕过 DRF 的分发就等于绕过了被断言的东西。
    认证/权限**置空**：本探针要隔离的是序列化器那一段，不是准入。

    落库这一段刻意照计划 Task 6 的写路径写：`setattr` + `save()`，**不调 `full_clean()`**
    ——序列化器一旦放行，越界的 int 就是这么进列的（这也是 `DataError` → HTML 500 的来路）。
    """

    authentication_classes = ()
    permission_classes = ()

    def post(self, request):
        serializer = AccountWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        signup = {
            "username": data.pop("username"),
            "password": "pw-123456",
            "first_name": data.pop("display_name", ""),
        }
        data.pop("role", None)
        data.pop("games", None)
        user = User.objects.create_user(**signup)
        profile = StaffProfile(user=user)
        for name, value in data.items():
            setattr(profile, name, value)
        profile.save()
        return Response({"ok": True, "user_id": user.pk})


def _valid_payload(game_id, **overrides):
    # ⚠️ 六个「必填，照抄」字段（R13）必须齐全：本函数是**新增**（create）形状的载荷，
    #    缺任何一个都会被 `AccountWriteSerializer` 拒成 1000。
    data = {
        "username": "newbie",
        "display_name": "小王",
        "role": "客服",
        "games": [game_id],
        "commission_value": "10.00",
        "deposit_amount": "0.00",
        "grab_limit_dl": 0,
        "grab_limit_pw": 0,
        "hired_on": "2026-01-02",
    }
    data.update(overrides)
    return data


class AccountWriteSerializerTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        # 第二个租户必须在 setUpClass 建（测试内建 schema 与外层事务相冲）
        super().setUpClass()
        # ：`role` 改为**请求期**查 `Role` 表 ⇒ 「客服」这类内置职位
        # 必须真的作为一行存在于本租户。`TenantTestCase` 只建空 schema、**不灌基线数据**
        # （Group / Role / admin 都没有），不在这里 `seed_tenant` 的话，
        # `test_valid_payload_passes` 这类既有用例会因「租户里没有 `客服` 这一行」而红——
        # 那是夹具缺基线，不是校验写错了。本调用与生产开通流程
        # （`provisioning.provision_tenant` → `seed_tenant`）是同一入口。
        seed_tenant(cls.tenant)
        from apps.platform.provisioning import provision_tenant

        cls.tenant_b, _ = provision_tenant(
            schema_name="acct_ser_b", name="租户 B", domain="acct-ser-b.test.local"
        )

    def setUp(self):
        self.game = Game.objects.create(name="王者荣耀")

    def test_valid_payload_passes(self):
        s = AccountWriteSerializer(data=_valid_payload(self.game.pk))
        self.assertTrue(s.is_valid(), s.errors)

    def test_create_marks_each_missing_spec_required_field(self):
        """R13 / spec §3.2 / §4.2：六个「必填，照抄」字段在**新增**上逐个缺一不可。

        逐个删是承重点：只钉其中一个的话，另外五个的必填性没有被任何断言摸到。
        错误必须挂在**该字段自己的键**下——「拒了但说不清是谁」不是这条规则要的形状。
        """
        base = _valid_payload(self.game.pk)
        for field in CREATE_REQUIRED_FIELDS:
            payload = {key: value for key, value in base.items() if key != field}
            with self.subTest(field=field):
                s = AccountWriteSerializer(data=payload)
                self.assertFalse(s.is_valid(), f"{field} 缺席居然通过了")
                self.assertIn(field, s.errors)

    def test_edit_paths_still_allow_omitting_the_same_six_fields(self):
        """上一条的边界：六个字段的必填**只属于新增**，编辑的两种形状都不受影响。

        - `PATCH`（`partial=True`）；
        - `PUT`（`partial=False` 但带既存实例）。

        判据必须是「有没有既存实例」，不能只看 `self.partial`——`PUT` 也不是 partial，
        把它一起收进必填会打红 `AccountUpdateTests` 里既有的整单替换用例。
        """
        user = User.objects.create_user(username="six-edit", password="pw-123456")
        patch = AccountWriteSerializer(
            data={"username": "six-edit"}, partial=True, context={"instance": user}
        )
        self.assertTrue(patch.is_valid(), patch.errors)
        put = AccountWriteSerializer(
            data={"username": "six-edit", "role": "客服", "games": [self.game.pk]},
            partial=False,
            context={"instance": user},
        )
        self.assertTrue(put.is_valid(), put.errors)

    def test_games_must_be_non_empty(self):
        s = AccountWriteSerializer(data=_valid_payload(self.game.pk, games=[]))
        self.assertFalse(s.is_valid())
        self.assertIn("games", s.errors)

    def test_games_from_another_tenant_are_rejected(self):
        """跨租户外键入口：别的 schema 的 game id 在本 schema 查不到 → 必须拒。"""
        from django_tenants.utils import tenant_context

        # 两个 schema 的 `business_game` 序列**各自从 1 开始**（实测：同一 id 在两边都存在），
        # 所以这里显式把 B 的 id 定成「A 现有最大 id + 1」。否则这条测试的判别力会取决于
        # 「哪几条测试先跑、A 的序列推进到几」——顺序一变，A 里恰好也有同 id 的游戏，
        # 「拒」就失真了。
        foreign_id = (
            Game.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
        ) + 1
        with tenant_context(self.tenant_b):
            Game.objects.create(id=foreign_id, name="只属于 B 的游戏")
            # 正向对照的一半：这个 id 在 B 里确实存在
            self.assertTrue(Game.objects.filter(pk=foreign_id).exists())
        # 另一半：同一个 id 在 A 的 schema 里查不到——下面的「拒」必须出自隔离，
        # 而不是出自「B 里压根没造出来」
        self.assertFalse(Game.objects.filter(pk=foreign_id).exists())

        s = AccountWriteSerializer(data=_valid_payload(foreign_id))
        self.assertFalse(s.is_valid())
        self.assertIn("games", s.errors)

    def test_a_tenant_created_role_is_accepted(self):
        """2026-09-23（本线 D1）：自建职位**必须被接受**。

        原用例名是 `test_role_outside_default_roles_is_rejected`（D8 的范围裁剪），
        它的口径已被业务方显式反转。⚠️ 夹具要**真的建一条 `Role`**——
        本线的判据是「租户里存在这个角色」，不是「名字不在五个常量里」。
        """
        Role.objects.create(name="夜班客服", group=Group.objects.create(name="夜班客服"))
        s = AccountWriteSerializer(data=_valid_payload(self.game.pk, role="夜班客服"))
        self.assertTrue(s.is_valid(), s.errors)

    def test_unknown_role_is_rejected(self):
        """不存在的名字仍要拒 —— 且必须是**字段级**错误（`role` 键），不是整条 500。"""
        s = AccountWriteSerializer(data=_valid_payload(self.game.pk, role="根本没有这个职位"))
        self.assertFalse(s.is_valid())
        self.assertIn("role", s.errors)

    def test_negative_amounts_are_rejected(self):
        # 薪酬/限额字段一律 >= 0（与订单金额的 D-0015 无关，见 Global Constraints）
        for field in ("commission_value", "deposit_amount", "base_salary",
                      "grab_price_min", "grab_price_max"):
            with self.subTest(field=field):
                s = AccountWriteSerializer(data=_valid_payload(self.game.pk, **{field: "-1.00"}))
                self.assertFalse(s.is_valid())
                self.assertIn(field, s.errors)

    def test_amounts_zero_and_positive_are_accepted(self):
        """上一条的正向对照：这些字段不是「一律拒」，而是「>= 0 放行」。

        没有这条，一个把五个字段全写死的实现也能让负值测试全绿。
        """
        s = AccountWriteSerializer(
            data=_valid_payload(
                self.game.pk,
                commission_value="0.00",
                deposit_amount="12.34",
                base_salary="5000.00",
                grab_price_min="1.00",
                grab_price_max="2.00",
            )
        )
        self.assertTrue(s.is_valid(), s.errors)

    def test_equal_price_bounds_are_allowed(self):
        """区间边界与模型 CHECK 同口径（`>=`），等值必须放行。"""
        s = AccountWriteSerializer(
            data=_valid_payload(self.game.pk, grab_price_min="10.00", grab_price_max="10.00")
        )
        self.assertTrue(s.is_valid(), s.errors)

    def test_inverted_price_bounds_are_rejected(self):
        s = AccountWriteSerializer(
            data=_valid_payload(self.game.pk, grab_price_min="100.00", grab_price_max="1.00")
        )
        self.assertFalse(s.is_valid())
        self.assertIn("grab_price_max", s.errors)

    def test_order_by_boundaries(self):
        """`order_by`（排序值）的输入区间是 `0..9999`。

        上界 9999 来自对标输入框的 `maxlength=4`；模型是 int4、不自带上界，
        所以越界值只能在这里挡住（与 `grab_limit_dl` / `grab_limit_pw` 同口径）。
        ⚠️ `0` **必须通过**（E-0021）：0 是「默认排序值」这个有意义的合法取值，
        用真值判断会把它当成「没传」而静默丢掉。
        """
        for value in (0, 9999):
            with self.subTest(value=value):
                s = AccountWriteSerializer(data=_valid_payload(self.game.pk, order_by=value))
                self.assertTrue(s.is_valid(), s.errors)
                self.assertEqual(s.validated_data["order_by"], value)
        for value in (10000, -1):
            with self.subTest(value=value):
                s = AccountWriteSerializer(data=_valid_payload(self.game.pk, order_by=value))
                self.assertFalse(s.is_valid())
                self.assertIn("order_by", s.errors)

    def test_settle_forbidden_accepts_both_booleans_and_rejects_others(self):
        """`settle_forbidden`（禁止结算）的两个布尔都必须是合法输入。

        ⚠️ `False` **必须通过**（E-0021）：它不是「没传」，而是「允许结算」这个明确的值。
        判据看 DRF 的 `is_valid()` 真假，而不是只看错误文本。
        """
        for value in (True, False):
            with self.subTest(value=value):
                s = AccountWriteSerializer(
                    data=_valid_payload(self.game.pk, settle_forbidden=value)
                )
                self.assertTrue(s.is_valid(), s.errors)
                self.assertIs(s.validated_data["settle_forbidden"], value)
        bad = AccountWriteSerializer(
            data=_valid_payload(self.game.pk, settle_forbidden="maybe")
        )
        self.assertFalse(bad.is_valid())
        self.assertIn("settle_forbidden", bad.errors)

    def test_two_new_keys_are_optional_and_deleted_at_is_not_writable(self):
        """这两个键**不是**必填（不进 `CREATE_REQUIRED_FIELDS`），
        且 `deleted_at` 根本不进序列化器——软删走专门端点，不许经接口改删除时间。
        """
        s = AccountWriteSerializer(data={}, partial=True)
        self.assertTrue(s.is_valid(), s.errors)
        # 缺席时不得凭空塞默认值：库里怎么存由模型默认值决定，不在这一层造值。
        self.assertNotIn("order_by", s.validated_data)
        self.assertNotIn("settle_forbidden", s.validated_data)
        self.assertNotIn("deleted_at", AccountWriteSerializer().fields)
        self.assertNotIn("order_by", CREATE_REQUIRED_FIELDS)
        self.assertNotIn("settle_forbidden", CREATE_REQUIRED_FIELDS)

    def test_forbid_self_service_is_written_and_read_back(self):
        """写入 + 回读：POST 带 `grab_forbid_self_service=True` ⇒ 档案行落 `True`。

        走 `_WriteProbeView`（项目真实的写路径形状：`setattr` + `save()`），
        断言打在**数据库行**上（E-0009）。
        """
        request = APIRequestFactory().post(
            "/api/accounts/",
            _valid_payload(self.game.pk, grab_forbid_self_service=True),
            format="json",
        )
        response = _WriteProbeView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        stored = StaffProfile.objects.get(user_id=response.data["user_id"])
        self.assertIs(stored.grab_forbid_self_service, True)

    def test_forbid_self_service_absent_key_stays_out_of_validated_data(self):
        """不传 ⇒ `validated_data` 里**没有这个键**（`required=False`，且不给 `default=`）。

        编辑路径靠这一点保持库里的原值：写路径只赋「请求里出现过的键」。给这个字段加
        `default=False` 会让每个 PATCH 都把它重置成 `False`（静默关掉别人的开关）。
        """
        user = User.objects.create_user(username="forbid-edit", password="pw-123456")
        s = AccountWriteSerializer(
            data={"display_name": "改名"}, partial=True, context={"instance": user}
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn("grab_forbid_self_service", s.validated_data)

    def test_server_managed_fields_are_rejected_not_ignored(self):
        """编辑传这几个必须**拒**（spec §7.4），不是静默忽略。

        ⚠️ `deleted_at` 是 Task 8加进 `SERVER_MANAGED_FIELDS` 的
        （T2 评审移交）：它原先既不在字段声明里、也不在那个元组里 ⇒
        `PATCH {"deleted_at": …}` 是**静默 no-op 还回 code 0**、库里不变、没有任何错误。
        """
        for field, value in (("password", "x"), ("is_superuser", True), ("is_staff", True),
                             ("is_active", False),
                             ("deleted_at", "2026-01-02T03:04:05Z")):
            with self.subTest(field=field):
                s = AccountWriteSerializer(data=_valid_payload(self.game.pk, **{field: value}))
                self.assertFalse(s.is_valid())
                # 拒绝必须记在这几个字段名下——否则「拒了」也可能只是别的字段报的错
                self.assertIn(field, s.errors)

    def test_tag_boundary_is_100_accept_and_101_reject(self):
        """标记列加宽到 100（Task 8 / T4 评审移交）：**100 字接受、101 字拒**。

        ⚠️ 数字写**字面值**——不引 `StaffProfile._meta` 的 `max_length`、也不引序列化器的
        字段常量：自指断言（拿被断言的符号当期望值）在被改坏时**不会红**（E-0024）。
        出处：对标的「设置标记」输入框是 `maxlength: 100` ⇒ 加宽后端（模型 + 序列化器 +
        `AlterField` 迁移），**不收窄**前端那个 `maxlength`（那会砍掉对标的产品能力）。
        """
        accepted = AccountWriteSerializer(data=_valid_payload(self.game.pk, tag="标" * 100))
        self.assertTrue(accepted.is_valid(), accepted.errors)
        self.assertEqual(len(accepted.validated_data["tag"]), 100)

        rejected = AccountWriteSerializer(data=_valid_payload(self.game.pk, tag="标" * 101))
        self.assertFalse(rejected.is_valid())
        self.assertIn("tag", rejected.errors)

    def test_duplicate_username_is_rejected(self):
        User.objects.create_user(username="newbie", password="pw-123456")
        s = AccountWriteSerializer(data=_valid_payload(self.game.pk))
        self.assertFalse(s.is_valid())
        self.assertIn("username", s.errors)

    def test_partial_update_does_not_require_every_field(self):
        user = User.objects.create_user(username="editme", password="pw-123456")
        s = AccountWriteSerializer(data={"display_name": "改名"}, partial=True,
                                  context={"instance": user})
        self.assertTrue(s.is_valid(), s.errors)

    def test_partial_update_allows_keeping_own_username(self):
        """编辑时不改 username 不得被自己的唯一性校验挡住。"""
        user = User.objects.create_user(username="keepme", password="pw-123456")
        s = AccountWriteSerializer(data={"username": "keepme"}, partial=True,
                                  context={"instance": user})
        self.assertTrue(s.is_valid(), s.errors)

    def test_partial_update_compares_against_the_stored_bound(self):
        """PATCH 只发一端时，另一端沿用**库里**的值再比大小。

        只看本次请求会放过「新上限 < 库里下限」：那条请求会落到模型 CHECK
        `staffprofile_grab_price_range_ordered`（IntegrityError），调用方拿到的
        不是「上限不能小于下限」这条可读错误。
        """
        user = User.objects.create_user(username="range-edit", password="pw-123456")
        StaffProfile.objects.create(user=user, grab_price_min=Decimal("100.00"))
        s = AccountWriteSerializer(
            data={"grab_price_max": "1.00"}, partial=True, context={"instance": user}
        )
        self.assertFalse(s.is_valid())
        self.assertIn("grab_price_max", s.errors)

    def test_partial_update_accepts_a_bound_above_the_stored_one(self):
        """上一条的正向对照：同形状请求，只把新上限抬到库里下限之上。"""
        user = User.objects.create_user(username="range-edit-ok", password="pw-123456")
        StaffProfile.objects.create(user=user, grab_price_min=Decimal("100.00"))
        s = AccountWriteSerializer(
            data={"grab_price_max": "200.00"}, partial=True, context={"instance": user}
        )
        self.assertTrue(s.is_valid(), s.errors)

    def test_drf_instance_kwarg_is_also_honoured(self):
        """`instance=`（DRF 标准入参）与 `context["instance"]` 两种写法都要认。

        只认后者的话，将来任何一次「改回 DRF 惯例写法」都会让
        `validate_username` 把账号自己判成重名（而价格区间检查是**静默**失效的）。
        """
        user = User.objects.create_user(username="kw-instance", password="pw-123456")
        s = AccountWriteSerializer(data={"username": "kw-instance"}, partial=True, instance=user)
        self.assertTrue(s.is_valid(), s.errors)


class AccountFilterSerializerTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        # ：筛选与写表单**共用**请求期存在性校验 ⇒ 与
        # `AccountWriteSerializerTests` 同一个前提：本租户的 `Role` 表里要有内置职位行，
        # 否则 `{"role": "客服"}` 的正向用例会因为夹具没灌基线而红。
        super().setUpClass()
        seed_tenant(cls.tenant)

    def test_malformed_filters_are_rejected_not_500(self):
        for payload in ({"is_active": "maybe"}, {"role": "不存在的职位"}):
            with self.subTest(payload=payload):
                s = AccountFilterSerializer(data=payload)
                self.assertFalse(s.is_valid())

    def test_page_is_not_this_serializers_business(self):
        """`page` 由 `EnvelopePagination` 管（它把非法值翻成 1000，见 Task 4）。

        这里把「它不在这张表里」写成断言，而不是 `continue` 跳过——跳过等于该分支
        什么都没验，读起来像漏了一个筛选参数。
        """
        s = AccountFilterSerializer(data={"page": "abc"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn("page", s.validated_data)  # 也不能透传给 list_accounts(**…)

    def test_valid_filters_pass(self):
        """上一条的正向对照：合法筛选值（含布尔字符串）必须放行且已转型。"""
        s = AccountFilterSerializer(data={"role": "客服", "is_active": "true", "key": "小王"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIs(s.validated_data["is_active"], True)
        # ⚠️ 「选择职位」改成多选 ⇒ `role` 的出参是**列表**（`["客服"]`），
        # 不是字符串。HTTP 出口上的单值行为没变（`groups__name__in=["客服"]` 与
        # `groups__name="客服"` 结果相同，既有端点用例全部照旧）。
        self.assertEqual(s.validated_data["role"], ["客服"])

    def test_comma_separated_roles_are_split_and_each_validated(self):
        """多选：前端拼的 `"客服,代练员"` ⇒ 出参两个职位；其中一个非法 ⇒ **整条拒绝**。"""
        ok = AccountFilterSerializer(data={"role": "客服,代练员"})
        self.assertTrue(ok.is_valid(), ok.errors)
        self.assertEqual(ok.validated_data["role"], ["客服", "代练员"])

        bad = AccountFilterSerializer(data={"role": "客服,不存在的职位"})
        self.assertFalse(bad.is_valid())

    def test_empty_filter_set_is_valid(self):
        """三个筛选参数都可缺省（列表接口不带参数也要能用）。"""
        s = AccountFilterSerializer(data={})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data, {})


class AccountBatchStatusSerializerTests(SimpleTestCase):
    """spec §9-8：单次批量启停 `ids` 上限 200，超限 → 1000。

    边界两头都要钉：只测「201 被拒」的话，把上限写成 0（或 `allow_empty=False` 退化）
    的实现也能全绿；只测「200 放行」则漏掉上限压根没生效。两者合起来才唯一确定边界
    落在 200 上。
    """

    #: 直接声明边界值（不 import 实现里的常量）：实现把 200 改小时，本用例必须自己变红。
    CAP = 200

    def test_exactly_the_cap_is_accepted(self):
        s = AccountBatchStatusSerializer(
            data={"ids": list(range(1, self.CAP + 1)), "is_active": False}
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(len(s.validated_data["ids"]), self.CAP)

    def test_one_over_the_cap_is_rejected(self):
        s = AccountBatchStatusSerializer(
            data={"ids": list(range(1, self.CAP + 2)), "is_active": False}
        )
        self.assertFalse(s.is_valid())
        # 错误必须挂在 `ids` 下——否则「拒了」可能只是别的字段报的错。
        self.assertIn("ids", s.errors)

    def test_a_small_batch_is_still_accepted(self):
        """正向对照的常态形状：小批量不受上限影响（上限不是「一律拒」）。"""
        s = AccountBatchStatusSerializer(data={"ids": [1, 2, 3], "is_active": True})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["ids"], [1, 2, 3])


class AccountPayloadTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="王者荣耀")
        self.user = User.objects.create_user(username="payload-user", password="pw-123456")
        self.user.first_name = "小王"
        self.user.save()
        self.user.groups.add(Group.objects.create(name="客服"))
        profile = StaffProfile.objects.create(
            user=self.user, commission_value=Decimal("10.00")
        )
        profile.games.add(self.game)

    def test_payload_carries_identity_role_games_and_profile(self):
        payload = account_payload(self.user)
        self.assertEqual(payload["username"], "payload-user")
        self.assertEqual(payload["display_name"], "小王")
        self.assertEqual(payload["role"], "客服")
        self.assertEqual(payload["games"], [self.game.pk])
        self.assertEqual(payload["commission_value"], Decimal("10.00"))

    def test_payload_never_carries_a_password_shaped_key(self):
        """spec §5.2：密码只在新增加密响应里出现一次；输出形状不得含它的任何形态。"""
        payload = account_payload(self.user)
        self.assertTrue(payload, "空载荷会让下面的否定断言恒真")
        self.assertIn("username", payload)  # 正向对照：确实取到了账号
        leaked = sorted(k for k in payload if "password" in k.lower())
        self.assertEqual(leaked, [])

    def test_payload_without_a_profile_still_renders(self):
        """可达路径：`seed_tenant` 灌的 `admin` 没有档案行，但它会出现在列表里。"""
        plain = User.objects.create_user(username="no-profile", password="pw-123456")
        payload = account_payload(plain)
        self.assertEqual(payload["username"], "no-profile")  # 正向对照：载荷本身非空
        self.assertEqual(payload["games"], [])
        self.assertNotIn("commission_value", payload)


class AccountErrorCodeTests(SimpleTestCase):
    def test_new_codes_are_appended_without_renumbering(self):
        """1003/1004 是对外契约（前端按数字分支），追加不得改动既有码。"""
        self.assertEqual(ErrorCode.OK, 0)
        self.assertEqual(ErrorCode.VALIDATION, 1000)
        self.assertEqual(ErrorCode.CONFLICT, 1001)
        self.assertEqual(ErrorCode.BUSINESS, 1002)
        self.assertEqual(ErrorCode.LAST_ADMIN, 1003)
        self.assertEqual(ErrorCode.SELF_LOCKOUT, 1004)


class GrabLimitInt4BoundTests(TenantTestCase):
    """`grab_limit_dl` / `grab_limit_pw` 必须有 int4 上界（E-0010 的判据）。

    模型字段是 `PositiveIntegerField` → PostgreSQL `integer`（实测 `db_type == "integer"`，
    字段自带 `MaxValueValidator(2147483647)`），但那两个校验器**只在 `full_clean()` 里跑**；
    本线的写路径是 `setattr` + `save()`，不调 `full_clean()`。所以不在这里挡住的话，
    `3000000000` 会一路落到 int4 列上，抛 `DataError`（SQLSTATE 22003）→ HTML 500。
    """

    @classmethod
    def setUpClass(cls):
        # ：`role` 改请求期查库后，`_valid_payload` 的默认角色「客服」
        # 必须真的作为一行 `Role` 存在 —— `TenantTestCase` 不灌基线数据，同
        # `AccountWriteSerializerTests.setUpClass` 的理由。
        super().setUpClass()
        seed_tenant(cls.tenant)

    def setUp(self):
        self.game = Game.objects.create(name="王者荣耀")

    def test_above_the_int4_column_is_rejected(self):
        s = AccountWriteSerializer(
            data=_valid_payload(
                self.game.pk, grab_limit_dl=INT4_MAX + 1, grab_limit_pw=INT4_MAX + 1
            )
        )
        self.assertFalse(s.is_valid())
        # 错误必须记在这两个字段名下，而不是「拒了但说不清是谁」
        self.assertIn("grab_limit_dl", s.errors)
        self.assertIn("grab_limit_pw", s.errors)

    def test_at_the_int4_max_is_accepted(self):
        """上一条的正向对照：正好等于列上界的值必须放行。

        没有这条，一个 `max_value=0` 的实现（或把这两个字段整个禁掉的实现）也能让负例全绿。
        """
        s = AccountWriteSerializer(
            data=_valid_payload(
                self.game.pk, grab_limit_dl=INT4_MAX, grab_limit_pw=INT4_MAX
            )
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["grab_limit_dl"], INT4_MAX)
        self.assertEqual(s.validated_data["grab_limit_pw"], INT4_MAX)

    def test_rejection_is_a_1000_not_a_500(self):
        """拒绝必须落在项目真实的出口上：HTTP 200 + `code 1000`，不是 HTML 500。"""
        request = APIRequestFactory().post(
            "/api/accounts/",
            _valid_payload(self.game.pk, grab_limit_dl=INT4_MAX + 1),
            format="json",
        )
        response = _WriteProbeView.as_view()(request)
        self.assertNotEqual(response.status_code, 500)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], int(ErrorCode.VALIDATION))

    def test_in_range_value_reaches_the_column_as_success(self):
        """上一条的正向对照：同一条管线在合法值上必须真的落到数据库行上。

        它同时钉住上界的**取值**：`INT4_MAX` 是列真正接受的最大值（写进去读得回来），
        所以 `max_value` 与列同界这件事不是从实现里抄来的，而是被写路径证过的。
        没有这条，探针视图自己写坏（比如无条件抛异常）也会让上一条一直「通过」。
        """
        request = APIRequestFactory().post(
            "/api/accounts/",
            _valid_payload(self.game.pk, grab_limit_dl=INT4_MAX, grab_limit_pw=INT4_MAX),
            format="json",
        )
        response = _WriteProbeView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        stored = StaffProfile.objects.get(user_id=response.data["user_id"])
        self.assertEqual(stored.grab_limit_dl, INT4_MAX)
        self.assertEqual(stored.grab_limit_pw, INT4_MAX)
