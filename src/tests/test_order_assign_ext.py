"""`/assign/` 与 `/clear-taker/` 的本批扩展（spec §5.1 / §5.2 / §6 / §8 第 2–5、16 条 + Z6）。

三类判据：① 三个新入参落到**数据库行**上；② 边界与失败形状（`1000` 且**库不变**）；
③ 金额归零只有一个实现（`zero_performance_amounts`）且两个触发点效果一致。

⚠️ 期望值一律写字面值；留痕/回退断言打在**数据库行**上（E-0024 / AGENTS §5）。
⚠️ 跨租户断言用**单号**指认，不用主键（两个 schema 的主键序列各自从 1 开始）。
⚠️ 与计划的偏差：租户 B 的域名改用 `.test.local` 后缀——`.env` 的
   `DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,.test.local` 不含 `.test.com`，
   计划里的 `oas-tenant-b.test.com` 会被 Django 断成 `DisallowedHost`（400）。
"""
from decimal import Decimal

from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
    SettleStatus,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

BASE = "/api/business/orders"
NODES = (
    "order.assign_taker",
    "order.clear_taker",
    "order.submit_payout_price",
    "order.change_status",
    "order.list",
)


class _AssignFixture(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.taker = self._user("ext-taker", commission="20.00")
        self.client = make_logged_in_client(
            username="ext-cs", groups=("客服",), nodes=NODES
        )

    def _user(self, username, commission="20.00"):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(username=username)
        profile = StaffProfile.objects.create(
            user=user,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal(commission),
        )
        # ⭐ D-0252：`assign_order` 入口多了「目标打手游戏范围」闸
        #    （无档案=无游戏=拒）⇒ 本夹具造的打手一律持本类的游戏，让那一闸放行
        #    （「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        profile.games.set([self.game])
        return user

    def _order(self, no="20260923000000000501", *, status=OrderStatus.UNASSIGNED, **kw):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            hall_price=Decimal("100.00"),
            **kw,
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("20.00")
        )
        return order

    def _assign(self, order, payload):
        return self.client.post(
            f"{BASE}/{order.order_no}/assign/", payload, content_type="application/json"
        ).json()

    def _clear(self, order, payload=None):
        return self.client.post(
            f"{BASE}/{order.order_no}/clear-taker/",
            payload if payload is not None else {},
            content_type="application/json",
        ).json()

    def _perf(self, order):
        return Performance.objects.get(order=order, emp_type=EmpType.MAIN)

    def _assigned(self, no, **kw):
        """造一张 `IN_PROGRESS` + 有接单人/金额的 MAIN 行（`isDelEmp` 两档的共用夹具）。"""
        order = self._order(no=no)
        order.status = OrderStatus.IN_PROGRESS
        order.save(update_fields=["status"])
        perf = self._perf(order)
        perf.user = self.taker
        perf.temp_amount = Decimal("66.00")
        perf.bonus_amount = Decimal("44.00")
        perf.taker_price = Decimal("55.00")
        perf.hours = 12
        perf.project = "三角洲"
        perf.save(
            update_fields=[
                "user", "temp_amount", "bonus_amount", "taker_price", "hours", "project",
            ]
        )
        return order, perf

    def _order_with_taker(self, no, taker, *, status=OrderStatus.COMPLETED):
        """造一张「指定状态 + **已有**接单人 + 六列都有值」的单。

        R-2b 的判据要旧接单人的覆盖价 / 奖金**真的有值**（否则「被清掉」与
        「本来就没有」分不开）；状态默认 `COMPLETED`——`/assign/` 的**自环**（→ 同状态）
        在状态机里仍是非法边，而 R-2b 描述的是「单子还能再次保存指派」那条路。
        """
        order = self._order(no=no, status=status)
        perf = self._perf(order)
        perf.user = taker
        perf.claimed_at = timezone.now()
        perf.review_status = ReviewStatus.APPROVED
        perf.bonus_amount = Decimal("44.00")
        perf.taker_price = Decimal("55.00")
        perf.hours = 12
        perf.project = "三角洲"
        perf.save(
            update_fields=[
                "user", "claimed_at", "review_status", "bonus_amount",
                "taker_price", "hours", "project",
            ]
        )
        return order, perf


class AssignNewFieldsTests(_AssignFixture):
    """spec §8 第 2、3 条：三个新入参只带值时才写；不带时保持服务端算出的值。"""

    def test_hours_project_and_fee_land_on_the_database_row(self):
        order = self._order()
        body = self._assign(
            order,
            {
                "user_id": self.taker.id,
                "fee": "88.50",
                "hours": 24,
                "project": "三角洲",
            },
        )
        self.assertEqual(body["code"], 0, body)
        perf = self._perf(order)
        self.assertEqual(perf.user_id, self.taker.id)
        self.assertEqual(perf.temp_amount, Decimal("88.50"))   # fee 覆盖服务端按提成算的值
        self.assertEqual(perf.hours, 24)
        self.assertEqual(perf.project, "三角洲")
        # 判据：`fee` 是**覆盖** `temp_amount`，不是 `taker_price` / 不是 `hall_price`
        self.assertIsNone(perf.taker_price)
        self.assertEqual(order.hall_price, Decimal("100.00"))

    def test_fee_does_not_touch_bonus_amount(self):
        order = self._order()
        perf = self._perf(order)
        perf.bonus_amount = Decimal("7.00")
        perf.save(update_fields=["bonus_amount"])
        body = self._assign(order, {"user_id": self.taker.id, "fee": "50.00"})
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._perf(order).bonus_amount, Decimal("7.00"))

    def test_without_the_new_keys_the_commission_math_still_decides_the_amount(self):
        """spec §8 第 3 条：不带三个新键 ⇒ `temp_amount` = 服务端按提成算出的值（既有口径不变）。"""
        order = self._order()
        body = self._assign(order, {"user_id": self.taker.id})
        self.assertEqual(body["code"], 0, body)
        perf = self._perf(order)
        self.assertEqual(perf.temp_amount, Decimal("20.00"))   # 100.00 × 20%（Order.hall_price × 提成率）
        self.assertIsNone(perf.hours)
        self.assertEqual(perf.project, "")


class AssignBoundaryTests(_AssignFixture):
    """spec §8 第 4 条：`fee` 的 0/负数合法；`hours` 与 `project` 的非法值 ⇒ `1000` 且库不变。"""

    def test_a_zero_and_a_negative_fee_are_both_accepted(self):
        cases = (
            ("0", Decimal("0.00"), "20260923000000000511"),
            ("-12.34", Decimal("-12.34"), "20260923000000000512"),
        )
        for value, want, no in cases:
            with self.subTest(fee=value):
                order = self._order(no=no)
                body = self._assign(order, {"user_id": self.taker.id, "fee": value})
                self.assertEqual(body["code"], 0, body)
                self.assertEqual(self._perf(order).temp_amount, want)

    def test_invalid_hours_is_rejected_with_1000_and_nothing_is_written(self):
        cases = ((0, "20260923000000000521"), (-1, "20260923000000000522"),
                 ("abc", "20260923000000000523"))
        for value, no in cases:
            with self.subTest(hours=value):
                order = self._order(no=no)
                body = self._assign(
                    order, {"user_id": self.taker.id, "hours": value, "project": "三角洲"}
                )
                self.assertEqual(body["code"], 1000, body)
                perf = self._perf(order)
                self.assertIsNone(perf.user_id)          # 整笔都没写（不是「只写了 hours 之外的部分」）
                self.assertIsNone(perf.hours)
                self.assertEqual(perf.project, "")
                order.refresh_from_db()
                self.assertEqual(order.status, OrderStatus.UNASSIGNED)

    def test_a_blank_project_is_rejected_with_1000_and_nothing_is_written(self):
        cases = (("", "20260923000000000531"), ("   ", "20260923000000000532"))
        for value, no in cases:
            with self.subTest(project=value):
                order = self._order(no=no)
                body = self._assign(
                    order, {"user_id": self.taker.id, "hours": 24, "project": value}
                )
                self.assertEqual(body["code"], 1000, body)
                perf = self._perf(order)
                self.assertIsNone(perf.user_id)
                self.assertIsNone(perf.hours)

    def test_a_project_longer_than_the_column_is_rejected_with_1000(self):
        # 列宽 900（E-0239）⇒ 超限输入用 901（129 在新列宽下是**合法**值，不再拒）。
        order = self._order(no="20260923000000000544")
        body = self._assign(order, {"user_id": self.taker.id, "project": "甲" * 901})
        self.assertEqual(body["code"], 1000, body)
        self.assertIsNone(self._perf(order).user_id)


class EnsureMainRowDoesNotRecreateTheCustomerRowTests(TenantTestCase):
    """FX2 第二根因（2026-10-28）：MAIN 行缺、客服行还在 ⇒ 补 MAIN 时⛔ 不许重造客服行。

    现场形态可达：B-1 把无主 MAIN 绩效行**整条**删掉，而客服行（销售归属）还在。
    旧实现 `ensure_main_performance_of` 直接调 `create_performance_for_order`（连客服行
    一起建）⇒ 撞 `uniq_performance_order_user` ⇒ `IntegrityError` ⇒ 代练通同步里
    「把接手人记成占位账号」整笔失败，接手人永远为空。
    """

    def test_restoring_a_missing_main_row_keeps_the_single_customer_row(self):
        from django.contrib.auth import get_user_model

        from apps.business.performance import ensure_main_performance_of

        game = Game.objects.create(name="原神")
        sales = get_user_model().objects.create_user(username="fx2-sales")
        order = Order.objects.create(
            game=game,
            order_no="20261028000000000901",
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("100.00"),
            sales_user=sales,
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.SALES, user=sales,
            temp_amount=Decimal("0.00"),
        )

        main = ensure_main_performance_of(order)

        self.assertEqual(main.emp_type, EmpType.MAIN)
        self.assertIsNone(main.user_id)
        self.assertEqual(main.temp_amount, Decimal("100.00"))
        self.assertEqual(Performance.objects.filter(order=order).count(), 2)
        self.assertEqual(
            Performance.objects.filter(order=order, emp_type=EmpType.SALES).count(),
            1,
            "客服行已经在了 ⇒ ⛔ 不许再建一条（唯一索引会拒）",
        )


class AssignFromAnyStatusTests(_AssignFixture):
    """T3–T6（D-0147 / spec §4.3）：`/assign/` 放开来源态之后的新语义。

    ⚠️ 这些用例是**新语义**的判据（先写红、再实现）；旧的「非 `UNASSIGNED` 必拒」
    断言在 `tests/test_dispatch_api.py` 里被按新语义重写。
    """

    #: 旧接单人（T4 的回退判据要它真的在库里有值）。
    OLD_TAKER = "ext-old-taker"

    def _completed_with_old_taker(self, no):
        """造一张「已完成 + 旧接单人 + 六列都有值」的单。"""
        return self._order_with_taker(
            no, self._user(self.OLD_TAKER, commission="30.00")
        )

    def test_assign_on_a_completed_order_moves_it_to_in_progress_and_switches_the_taker(self):
        """T3：`COMPLETED` 单可指派 ⇒ 状态转 `IN_PROGRESS`、接单人换成新的。"""
        order, perf = self._completed_with_old_taker("20260924000000000901")
        original_id = perf.id
        body = self._assign(order, {"user_id": self.taker.id})
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(perf.user_id, self.taker.id)
        self.assertEqual(perf.id, original_id)      # 复用同一行，不新建

    def test_switching_the_taker_rolls_back_the_previous_takers_columns(self):
        """T4：换人时**先回退再写** —— 旧接单人的六列被清（spec §4.3）。"""
        order, perf = self._completed_with_old_taker("20260924000000000902")
        # 前置哨兵：起点**真的有**这些值（否则下面「被清」在「本来就没有」时也成立）
        old_taker_id = perf.user_id
        self.assertIsNotNone(old_taker_id)
        self.assertEqual(perf.review_status, ReviewStatus.APPROVED)
        self.assertEqual(perf.bonus_amount, Decimal("44.00"))
        self.assertEqual(perf.taker_price, Decimal("55.00"))
        self.assertEqual(perf.hours, 12)
        self.assertEqual(perf.project, "三角洲")

        body = self._assign(order, {"user_id": self.taker.id})
        self.assertEqual(body["code"], 0, body)
        perf.refresh_from_db()
        self.assertEqual(perf.user_id, self.taker.id)
        self.assertIsNone(perf.review_status)
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))
        self.assertIsNone(perf.taker_price)
        self.assertIsNone(perf.hours)
        self.assertEqual(perf.project, "")

    def test_assign_with_a_price_recomputes_the_temp_amount_from_the_new_price(self):
        """T5：带 `price` ⇒ 写价**之后**再重算一次，`temp == 有效基价 × 提成率`。

        夹具里旧接单人有一条 `taker_price = 55.00`，而新接单人提成 20%：
        回退把旧覆盖价清掉 ⇒ 有效基价 = **新的发单价**。写价后不重算的话，
        `temp_amount` 会停在「旧发单价 100.00 × 20% = 20.00」——与 `hall_price` 对不上。
        """
        order, perf = self._completed_with_old_taker("20260924000000000903")
        body = self._assign(order, {"user_id": self.taker.id, "price": "80.00"})
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.hall_price, Decimal("80.00"))
        self.assertEqual(perf.temp_amount, Decimal("16.00"))    # 80.00 × 20%
        self.assertIsNone(perf.taker_price)                     # 旧覆盖价不参与计算

    def test_assign_without_a_price_still_recomputes_the_temp_amount(self):
        """T5 的另一半：不带 `price` ⇒ `temp_amount` 照旧按发单价 × 提成率重算。"""
        order, perf = self._completed_with_old_taker("20260924000000000904")
        perf.temp_amount = Decimal("7.77")                      # 一个明显陈旧的估值
        perf.save(update_fields=["temp_amount"])
        body = self._assign(order, {"user_id": self.taker.id})
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.hall_price, Decimal("100.00"))   # 价没被写
        self.assertEqual(perf.temp_amount, Decimal("20.00"))    # 100.00 × 20%

    def test_assign_revives_a_soft_deleted_main_row_instead_of_creating_a_new_one(self):
        """T6：MAIN 行被软删后再指派 ⇒ **复活同一行**（`uniq_order_main_taker` 只允许一行）。

        软删态用 **ORM 直接造**（裁定 P3），不等任务 3 的删除端点。
        """
        order = self._order(no="20260924000000000905")
        perf = self._perf(order)
        original_id = perf.id
        perf.deleted_at = timezone.now()
        perf.deleted_by = "测试删除人甲"
        perf.save(update_fields=["deleted_at", "deleted_by"])
        # 前置哨兵：库里**只有**这一条 MAIN 行，且它确实是软删态
        self.assertEqual(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).count(), 1
        )
        self.assertIsNotNone(Performance.objects.get(pk=original_id).deleted_at)

        body = self._assign(order, {"user_id": self.taker.id})
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).count(), 1
        )
        perf.refresh_from_db()
        self.assertEqual(perf.id, original_id)          # 同一行，不是新建
        self.assertIsNone(perf.deleted_at)
        self.assertEqual(perf.deleted_by, "")
        self.assertEqual(perf.user_id, self.taker.id)


class AssignSettledMainRowTests(_AssignFixture):
    """R-2a（D-0147 / spec §4.3 的更正块）：已结账的 MAIN 行上 `/assign/` 一律 `1014`。

    ⚠️ 判据是**三个字段的并集**（`settle_status` / `settled_at` / `settled_amount`），
    与 `performance_writes._is_settled` 同源。下面三条各只置**一个**字段：
    只看状态列的实现会被第 2、3 条抓住（存量行里两种形态都真实存在）。
    """

    #: 三个人造结账态各只落一个字段（`subTest` 的 id 是字段名）。
    SETTLED_SHAPES = (
        ("settle_status", {"settle_status": SettleStatus.SETTLED}),
        ("settled_at", {"settled_at": timezone.now()}),
        ("settled_amount", {"settled_amount": Decimal("123.00")}),
    )

    def test_assign_on_a_settled_main_row_is_1014_and_writes_nothing(self):
        """已结账 ⇒ `1014`，且 `hall_price`/`bonus_amount`/`user`/`status` 一行不写。

        ⚠️ 这条闸门**不看换不换人**：同一个人在这条路上也会走「写价 / 重算」，
        而 R6 的保护是「已结账的绩效不许直接改」⇒ 要让开路得先撤销结账。
        这里派的是**另一个人**（最危险的形态），并带 `price`（会改 `hall_price`）。
        """
        old_taker = self._user("ext-settled-old-taker", commission="30.00")
        for index, (field_name, shape) in enumerate(self.SETTLED_SHAPES):
            with self.subTest(settled_field=field_name):
                order, perf = self._order_with_taker(
                    f"2026092400000000091{index}", old_taker
                )
                old_user_id = perf.user_id
                old_temp = perf.temp_amount
                # 造态：**只**置这一个字段（其余两个保持建行时的初值）
                for key, value in shape.items():
                    setattr(perf, key, value)
                perf.save(update_fields=[*shape, "updated_at"])

                body = self._assign(order, {"user_id": self.taker.id, "price": "66.00"})

                self.assertEqual(body["code"], 1014, body)
                order.refresh_from_db()
                perf.refresh_from_db()
                # 一行不写：状态、发单价、覆盖价、奖金、工时、项目、归属人全不动
                self.assertEqual(order.status, OrderStatus.COMPLETED)
                self.assertEqual(order.hall_price, Decimal("100.00"))
                self.assertEqual(perf.user_id, old_user_id)
                self.assertIsNotNone(old_user_id)
                self.assertEqual(perf.taker_price, Decimal("55.00"))
                self.assertEqual(perf.bonus_amount, Decimal("44.00"))
                self.assertEqual(perf.temp_amount, old_temp)
                self.assertEqual(perf.hours, 12)
                self.assertEqual(perf.project, "三角洲")

    def test_assign_on_a_settled_main_row_is_1014_even_for_the_same_taker(self):
        """同一个人 + 已结账 ⇒ 仍拦（R-2a 在 R-2b 之前，不看是否换人）。"""
        order, perf = self._order_with_taker("20260924000000000920", self.taker)
        perf.settle_status = SettleStatus.SETTLED
        perf.save(update_fields=["settle_status", "updated_at"])
        body = self._assign(order, {"user_id": self.taker.id, "hours": 6})
        self.assertEqual(body["code"], 1014, body)
        perf.refresh_from_db()
        self.assertEqual(perf.hours, 12)                     # `hours` 也一个字没写
        self.assertEqual(perf.taker_price, Decimal("55.00"))
        self.assertEqual(perf.bonus_amount, Decimal("44.00"))


class AssignSameTakerTests(_AssignFixture):
    """R-2b（D-0147 / spec §4.3 的更正块）：同一个人再保存 ⇒ **跳过**七列回退。

    判据不是「回退没报错」，而是「**值还在**」：`taker_price` 覆盖价保留、`bonus_amount`
    不归零，且 `temp_amount` 仍等于「有效基价（覆盖价优先）× 提成率」。
    ⚠️ 若实现重新变成「无条件先 reset」，下面两条会以 `taker_price is None` /
    `temp_amount == 新发单价 × 提成率` 变红。
    """

    def test_saving_again_for_the_same_taker_keeps_his_override_price_and_bonus(self):
        """只改 `hours`：覆盖价 55.00 与奖金 44.00 保留，`temp == 55.00 × 20%`。"""
        order, perf = self._order_with_taker("20260924000000000930", self.taker)
        body = self._assign(order, {"user_id": self.taker.id, "hours": 6})
        self.assertEqual(body["code"], 0, body)
        perf.refresh_from_db()
        self.assertEqual(perf.user_id, self.taker.id)
        self.assertEqual(perf.hours, 6)
        self.assertEqual(perf.project, "三角洲")              # 没传就不动
        self.assertEqual(perf.taker_price, Decimal("55.00"))  # 覆盖价保留
        self.assertEqual(perf.bonus_amount, Decimal("44.00")) # 不归零
        self.assertEqual(perf.temp_amount, Decimal("11.00"))  # 55.00 × 20%（覆盖价优先）

    def test_saving_again_for_the_same_taker_with_a_new_price_keeps_the_override(self):
        """带 `price` 同人再保存：新价落 `hall_price`，但有效基价仍是覆盖价 ⇒ `temp == 11.00`。

        ⚠️ 判别力在于两个候选结果不同：跳过回退 ⇒ 11.00（55.00 × 20%）；
        无条件回退 ⇒ 16.00（80.00 × 20%）。断 11.00 的实现才是 R-2b。
        """
        order, perf = self._order_with_taker("20260924000000000931", self.taker)
        body = self._assign(order, {"user_id": self.taker.id, "price": "80.00"})
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.hall_price, Decimal("80.00"))
        self.assertEqual(perf.taker_price, Decimal("55.00"))
        self.assertEqual(perf.bonus_amount, Decimal("44.00"))
        self.assertEqual(perf.temp_amount, Decimal("11.00"))


class AssignWhileAlreadyInProgressTests(_AssignFixture):
    """R-3（2026-09-24 控制方裁定；spec §4.3 的 ⛔ 更正块）：单子**已经在「代练中」**时
    `/assign/` **跳过 `apply_transition`**，其余逻辑照旧。

    背景：`TRANSITIONS` 没有自环（`src == target` 不成边），而本动作的目标态就是
    `IN_PROGRESS` ⇒ 修复前这类单上保存指派一律被拒（`1002`），
    换人 / 改 `hours` / 改 `project` / 改发单价**全都做不了**——而「代练中」正是最常见状态。
    ⛔ 修法不是给状态机加自环：自环会经 `OrderSerializer.get_allowed_transitions` 泄漏成
    状态下拉里的「代练中 → 代练中」选项（读侧可见的行为变化）。

    判别力：修复前两条都拿到 `1002` 且库一行不动（下面每条都先钉「起点真有接单人、
    状态真是 `IN_PROGRESS`」的哨兵）；修复后 `code == 0` 且状态仍是 `IN_PROGRESS`。
    """

    #: 已经在「代练中」这单上的旧接单人（甲）。
    OLD_TAKER = "ext-inprogress-old-taker"

    def _already_in_progress(self, no):
        """造一张「代练中 + 已有接单人甲 + 六列都有值」的单。"""
        return self._order_with_taker(
            no,
            self._user(self.OLD_TAKER, commission="30.00"),
            status=OrderStatus.IN_PROGRESS,
        )

    def test_switching_the_taker_on_an_in_progress_order_succeeds(self):
        """判据 1：换人乙 ⇒ `0`、状态仍是 `IN_PROGRESS`、接单人变成乙、`temp == 100.00 × 20%`。

        ⚠️ `temp_amount` 断的是**回退之后**的口径：换人 ⇒ 甲的覆盖价 55.00 被回退，
        有效基价落回发单价 100.00 ⇒ 乙的提成 20% ⇒ 20.00。
        （不回退会得到 55.00 × 20% = 11.00，与 R-2b 的「同人才保留」相反。）
        """
        order, perf = self._already_in_progress("20260924000000000940")
        old_taker_id = perf.user_id
        # 前置哨兵：起点**真的有**接单人、状态**真的**是代练中（否则「换人成功」在
        # 「本来没人接」时也成立）
        self.assertIsNotNone(old_taker_id)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

        body = self._assign(order, {"user_id": self.taker.id})

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)   # 跳过状态机，状态不动
        self.assertEqual(perf.user_id, self.taker.id)
        self.assertNotEqual(perf.user_id, old_taker_id)
        self.assertIsNone(perf.taker_price)                       # 换人 ⇒ 旧覆盖价被回退
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))
        self.assertEqual(perf.temp_amount, Decimal("20.00"))      # 100.00 × 20%

    def test_saving_again_for_the_same_taker_on_an_in_progress_order_keeps_the_override(self):
        """判据 2：同一个人甲再保存（只改 `hours`/`project`/发单价）⇒ `0`，R-2b 覆盖价与奖金保留。

        ⚠️ 判别力在 16.50 vs 24.00：跳过回退 ⇒ 有效基价仍是覆盖价 55.00 ⇒ 55.00 × 30% = 16.50；
        无条件回退 ⇒ 80.00 × 30% = 24.00。`hours`/`project` 与 `price` 是**三个**可改键，
        本条一次过全改（同一支写入路径）。
        """
        order, perf = self._already_in_progress("20260924000000000941")
        old_taker_id = perf.user_id
        self.assertIsNotNone(old_taker_id)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

        body = self._assign(
            order,
            {
                "user_id": old_taker_id,
                "hours": 6,
                "project": "测试项目乙",
                "price": "80.00",
            },
        )

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)   # 跳过状态机，状态不动
        self.assertEqual(order.hall_price, Decimal("80.00"))      # 发单价照写
        self.assertEqual(perf.user_id, old_taker_id)              # 还是甲
        self.assertEqual(perf.hours, 6)
        self.assertEqual(perf.project, "测试项目乙")
        self.assertEqual(perf.taker_price, Decimal("55.00"))      # 覆盖价保留（R-2b）
        self.assertEqual(perf.bonus_amount, Decimal("44.00"))     # 奖金不归零（R-2b）
        self.assertEqual(perf.temp_amount, Decimal("16.50"))      # 55.00 × 30%（覆盖价优先）


class AssignOnOtherStatusesStillWalksTheStateMachineTests(_AssignFixture):
    """R-3 的判据 3（**反向对照**）：跳过**只**对 `IN_PROGRESS` 生效，别的状态仍走 `apply_transition`。

    为什么这条足以证伪「一刀切放行」：`WAITING_GRAB → IN_PROGRESS` 是一条**真边**，
    走状态机 ⇒ 库里 `status` 变成 `IN_PROGRESS`、`updated_by` 写成调用方；
    一刀切跳过 `apply_transition` ⇒ 单子**留在** `WAITING_GRAB`、`updated_by` 一个字不动
    ——两种实现在这两列上**取值不同**（不是「都给 `code 0`」那种分不开的形状）。
    调用账号**只**持 `order.assign_taker`（`/assign/` 自己的类级门），
    **不持** `grab_hall.take_order` / `order.change_status`：该边节点集含
    `order.assign_taker`，所以它过状态机的依据只有这一条，不由并集里别的成员兜着。
    """

    def test_assign_on_a_hall_order_still_transitions_it_to_in_progress(self):
        order = self._order(
            no="20260924000000000950", status=OrderStatus.WAITING_GRAB
        )
        # 前置哨兵：起点是大厅单，且 `updated_by` 还是空的
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(order.updated_by, "")
        self.client = make_logged_in_client(
            username="ext-assign-only",
            groups=("客服",),
            nodes=("order.assign_taker",),
        )
        taker = self._user("ext-assign-only-taker")

        body = self._assign(order, {"user_id": taker.id})

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        perf = self._perf(order)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)   # 跳过状态机 ⇒ 会留在大厅
        self.assertEqual(order.updated_by, "ext-assign-only")     # 由 `apply_transition` 写
        self.assertEqual(perf.user_id, taker.id)
        # 留痕照写（跳过状态机与留痕是两件事）
        self.assertTrue(
            OperationLog.objects.filter(
                order_no=order.order_no,
                action="performance.assign",
                actor="ext-assign-only",
            ).exists()
        )


class ClearTakerIsDelEmpTests(_AssignFixture):
    """spec §8 第 5 条 + Z6：`isDelEmp` 只决定金额，`hours`/`project` 两档都清。"""

    def test_the_default_and_n_keep_the_amounts_but_still_clear_hours_and_project(self):
        for payload in (None, {"isDelEmp": "N"}):
            with self.subTest(payload=payload):
                order, perf = self._assigned(
                    f"2026092300000000060{0 if payload is None else 1}"
                )
                body = self._clear(order, payload)
                self.assertEqual(body["code"], 0, body)
                perf.refresh_from_db()
                self.assertEqual(perf.temp_amount, Decimal("66.00"))     # N 档：预计绩效**保留**
                # ⚠️ `bonus_amount` 归零是 `reset_taker_performance` 的**既有**行为
                #    （回退三元组之一），本批不动它。
                self.assertEqual(perf.bonus_amount, Decimal("0.00"))
                self.assertIsNone(perf.hours)
                self.assertEqual(perf.project, "")
                self.assertIsNone(perf.taker_price)                      # 既有回退列照旧
                self.assertIsNone(perf.claimed_at)

    def test_y_zeroes_both_amounts_and_also_clears_hours_and_project(self):
        order, perf = self._assigned("20260923000000000610")
        body = self._clear(order, {"isDelEmp": "Y"})
        self.assertEqual(body["code"], 0, body)
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("0.00"))
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))
        self.assertIsNone(perf.user_id)
        self.assertIsNone(perf.review_status)
        self.assertIsNone(perf.taker_price)
        self.assertIsNone(perf.claimed_at)
        self.assertIsNone(perf.hours)
        self.assertEqual(perf.project, "")

    def test_y_zeroes_the_amount_even_when_no_main_row_exists_yet(self):
        """spec §5.2 / C15 + E-0027：**缺 MAIN 行的旧单**也必须归零（评审 A-Important-1）。

        机理：`apply_transition(UNASSIGNED)` 的钩子（`_on_pool_entry`）会
        `ensure_main_performance_of` **补建**那条行（新行 `temp_amount = hall_price`），
        所以「事务前查不到行」不等于「没有金额要归零」——旧实现把 `_perf` 的取值放在
        事务**之前**，于是这一档 `Y` 静默等价于 `N`（实测 `temp_amount` 仍是 `hall_price`）。
        ⚠️ 本用例断在**数据库行**上（E-0024），不看响应体里的金额。
        """
        order = Order.objects.create(
            game=self.game,
            order_no="20260923000000000630",
            status=OrderStatus.ISSUE,          # 允许 → UNASSIGNED 的来源态
            hall_price=Decimal("77.00"),
        )
        # 阳性对照的前置：这一单**真的**没有 MAIN 行（否则本用例测的是另一档）。
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).exists()
        )
        body = self._clear(order, {"isDelEmp": "Y"})
        self.assertEqual(body["code"], 0, body)
        perf = self._perf(order)               # 钩子补建出来的那条行
        self.assertEqual(perf.temp_amount, Decimal("0.00"))
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))
        # E-0027：同一条路径上 hours / project 的清理是否一致（两档都清）。
        self.assertIsNone(perf.hours)
        self.assertEqual(perf.project, "")
        self.assertIsNone(perf.user_id)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)

    def test_n_without_a_main_row_keeps_the_synthesized_estimate(self):
        """阴性对照（钉住 `isDelEmp` 的判别力）：缺行档送 `N` ⇒ 补建行的 `temp_amount` **保留**。"""
        order = Order.objects.create(
            game=self.game,
            order_no="20260923000000000631",
            status=OrderStatus.ISSUE,
            hall_price=Decimal("77.00"),
        )
        body = self._clear(order, {"isDelEmp": "N"})
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._perf(order).temp_amount, Decimal("77.00"))

    def test_an_invalid_value_is_rejected_with_1000_and_the_order_stays_assigned(self):
        order, perf = self._assigned("20260923000000000620")
        body = self._clear(order, {"isDelEmp": "X"})
        self.assertEqual(body["code"], 1000, body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(perf.user_id, self.taker.id)
        self.assertEqual(perf.temp_amount, Decimal("66.00"))
        self.assertEqual(perf.hours, 12)
        self.assertEqual(perf.project, "三角洲")


class MoneyChangingNodeGateTests(_AssignFixture):
    """必修 2（业务方 2026-09-24 裁定）：请求**真的会改钱**时额外要求节点
    `order.submit_payout_price`（1162）。

    两处口径：`/assign/` 传了 `fee` 时要求它（没传不需要，与既有「传 `price` 要 1162」
    同一条收口）；`/clear-taker/` 的 `isDelEmp=Y` 时要求它（`N` 不需要）。
    ⚠️ 判据走**节点**（`has_node_now`），不是 legacy codename；两处各带**阳性对照**：
    「同一个人不带钱 ⇒ 200」「持 1162 者带钱 ⇒ 200」——没有阳性对照，403 也可能是别的原因。
    """

    #: 只够进端点、但**不含** 1162 的节点集（端点的类级门 + 状态机边 + 读）。
    NO_MONEY_NODES = ("order.assign_taker", "order.clear_taker", "order.change_status", "order.list")

    def _client(self, username, nodes):
        # ⚠️ 两个坑各一条，别改成 `groups=()` 或内置职位名：
        # ① `groups` 不能空——节点挂在 `Group → Role → permissions` 上，没有组就一个节点也挂不上
        #    ⇒ 那样测出来的 403 是**类级门**拒的，不是本闸（假阳性）。
        # ② 组名**不能**用「客服」等内置职位：`_AssignFixture.setUp` 已经用同一个 Role 挂了
        #    1162，而节点是挂在**共享 Role**上的 ⇒ 本类的账号会白捡 1162，负向用例变恒真。
                #    自定义职位（非内置）才是那条真实缺口场景。
        return make_logged_in_client(username=username, groups=("闸门组",), nodes=nodes)

    def _post(self, client, order, action, payload):
        return client.post(
            f"{BASE}/{order.order_no}/{action}/", payload, content_type="application/json"
        )

    def test_assign_with_fee_requires_the_money_node(self):
        client = self._client("gate-assign", self.NO_MONEY_NODES)
        blocked = self._order(no="20260924000000000640")
        response = self._post(client, blocked, "assign", {"user_id": self.taker.id, "fee": "-999.00"})
        self.assertEqual(response.status_code, 403, response.content[:300])
        # 403 ⇒ **一行都不许写**（与既有 `price` 那道闸同口径，E-0022 同族）
        perf = self._perf(blocked)
        self.assertIsNone(perf.user_id)
        self.assertEqual(perf.temp_amount, Decimal("20.00"))
        blocked.refresh_from_db()
        self.assertEqual(blocked.status, OrderStatus.UNASSIGNED)

    def test_assign_without_fee_is_allowed_for_the_same_person(self):
        """阳性对照 1：同一个人（无 1162）**不带钱** ⇒ 200（本闸只对「真的会改钱」生效）。"""
        client = self._client("gate-assign-plain", self.NO_MONEY_NODES)
        order = self._order(no="20260924000000000641")
        body = self._post(client, order, "assign", {"user_id": self.taker.id}).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._perf(order).user_id, self.taker.id)

    def test_assign_with_fee_is_allowed_for_a_holder_of_the_money_node(self):
        """阳性对照 2：持 1162 者带 `fee` ⇒ 200，且金额**真的**落库。"""
        client = self._client(
            "gate-assign-rich",
            (*self.NO_MONEY_NODES, "order.submit_payout_price"),
        )
        order = self._order(no="20260924000000000642")
        body = self._post(client, order, "assign", {"user_id": self.taker.id, "fee": "-999.00"}).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._perf(order).temp_amount, Decimal("-999.00"))

    def test_clear_taker_with_is_del_emp_y_requires_the_money_node(self):
        client = self._client("gate-clear", self.NO_MONEY_NODES)
        order, perf = self._assigned("20260924000000000650")
        response = self._post(client, order, "clear-taker", {"isDelEmp": "Y"})
        self.assertEqual(response.status_code, 403, response.content[:300])
        # 403 ⇒ 状态与金额都不动
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(perf.user_id, self.taker.id)
        self.assertEqual(perf.temp_amount, Decimal("66.00"))
        self.assertEqual(perf.bonus_amount, Decimal("44.00"))

    def test_clear_taker_with_is_del_emp_n_is_allowed_for_the_same_person(self):
        """阳性对照 1：同一个人（无 1162）送 `isDelEmp=N` ⇒ 200（`N` 不改钱）。"""
        client = self._client("gate-clear-plain", self.NO_MONEY_NODES)
        order, perf = self._assigned("20260924000000000651")
        body = self._post(client, order, "clear-taker", {"isDelEmp": "N"}).json()
        self.assertEqual(body["code"], 0, body)
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("66.00"))      # N 档保留预计绩效

    def test_clear_taker_with_is_del_emp_y_is_allowed_for_a_holder_of_the_money_node(self):
        """阳性对照 2：持 1162 者送 `isDelEmp=Y` ⇒ 200 且两列归零。"""
        client = self._client(
            "gate-clear-rich",
            (*self.NO_MONEY_NODES, "order.submit_payout_price"),
        )
        order, perf = self._assigned("20260924000000000652")
        body = self._post(client, order, "clear-taker", {"isDelEmp": "Y"}).json()
        self.assertEqual(body["code"], 0, body)
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("0.00"))
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))


class CrossTenantAssignTests(TenantTestCase):
    """spec §8 第 16 条：跨租户用**单号**指认（主键在两个 schema 里各自从 1 开始）。"""

    SCHEMA_B = "oas_tenant_b"
    DOMAIN_B = "oas-tenant-b.test.local"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=cls.SCHEMA_B, name="指派线跨租户 B", domain=cls.DOMAIN_B
        )

    def setUp(self):
        from apps.rbac.seed import seed_catalog

        seed_catalog()
        game = Game.objects.create(name="跨租户-A 的游戏")
        self.order_a = Order.objects.create(
            game=game, order_no="20260923000000000701", status=OrderStatus.UNASSIGNED
        )
        Performance.objects.create(
            order=self.order_a, emp_type=EmpType.MAIN, temp_amount=Decimal("20.00")
        )
        self.taker_a = self._user("xt-taker-a")
        self.client_a = make_logged_in_client(
            username="xt-cs-a", groups=("客服",), nodes=NODES
        )
        try:
            with tenant_context(self.tenant_b):
                seed_catalog()
                game_b = Game.objects.create(name="跨租户-B 的游戏")
                self.order_b = Order.objects.create(
                    game=game_b,
                    order_no="20260923000000000702",
                    status=OrderStatus.UNASSIGNED,
                )
                Performance.objects.create(
                    order=self.order_b, emp_type=EmpType.MAIN, temp_amount=Decimal("30.00")
                )
        finally:
            from django.db import connection

            connection.set_tenant(self.tenant)

    def _user(self, username):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(username=username)

    def test_assign_on_another_tenants_order_number_is_1002_and_b_is_unchanged(self):
        body = self.client_a.post(
            f"{BASE}/20260923000000000702/assign/",
            {"user_id": self.taker_a.id, "hours": 24, "project": "三角洲"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1002, body)
        with tenant_context(self.tenant_b):
            perf = Performance.objects.get(order__order_no="20260923000000000702")
            self.assertIsNone(perf.user_id)
            self.assertIsNone(perf.hours)
            self.assertEqual(perf.project, "")

    def test_clear_taker_on_another_tenants_order_number_is_1002(self):
        body = self.client_a.post(
            f"{BASE}/20260923000000000702/clear-taker/",
            {"isDelEmp": "Y"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1002, body)
        with tenant_context(self.tenant_b):
            perf = Performance.objects.get(order__order_no="20260923000000000702")
            self.assertEqual(perf.temp_amount, Decimal("30.00"))

    def test_assign_on_another_tenants_completed_order_number_is_1002_and_b_is_unchanged(self):
        """新语义（放开来源态 + 带价重算 + 复活软删行）也要过跨租户这一关。

        判据用**订单号 / 用户名**指认 B 的那一行（两个 schema 的主键序列各自从 1 开始，
        用主键会命中「自己的那一行」——AGENTS.md §5）。这里给 B 的单一条
        `COMPLETED` + **有旧接单人**/覆盖价/金额的 MAIN 行：若跨租户没被挡住，
        它会被 `reset_taker_performance` 清掉、状态被拉回 `IN_PROGRESS`。
        不设接单人时「user 仍为空」恒真。这里给租户 B 一个合成用户，判据用用户名。
        """
        with tenant_context(self.tenant_b):
            game_b = Game.objects.get(name="跨租户-B 的游戏")
            order_b = Order.objects.create(
                game=game_b,
                order_no="20260923000000000703",
                status=OrderStatus.COMPLETED,
                hall_price=Decimal("120.00"),
            )
            b_taker = self._user("xt-taker-b")
            perf_b = Performance.objects.create(
                order=order_b,
                emp_type=EmpType.MAIN,
                user=b_taker,
                temp_amount=Decimal("120.00"),
                taker_price=Decimal("90.00"),
                bonus_amount=Decimal("30.00"),
            )
            perf_b_id = perf_b.id
            # 前置哨兵：旧接单人**真的**在库里（否则下面「没被换掉」在「本来就没有」时也成立）
            self.assertIsNotNone(perf_b.user_id)
            self.assertEqual(perf_b.user.username, "xt-taker-b")
        try:
            body = self.client_a.post(
                f"{BASE}/20260923000000000703/assign/",
                {"user_id": self.taker_a.id, "price": "66.00"},
                content_type="application/json",
            ).json()
            self.assertEqual(body["code"], 1002, body)
            with tenant_context(self.tenant_b):
                row = Order.objects.get(order_no="20260923000000000703")
                perf = Performance.objects.select_related("user").get(id=perf_b_id)
                self.assertEqual(row.status, OrderStatus.COMPLETED)
                self.assertEqual(row.hall_price, Decimal("120.00"))
                self.assertIsNotNone(perf.user_id)
                self.assertEqual(perf.user.username, "xt-taker-b")   # 旧接单人没被换掉
                self.assertEqual(perf.taker_price, Decimal("90.00"))
                self.assertEqual(perf.temp_amount, Decimal("120.00"))
                self.assertEqual(perf.bonus_amount, Decimal("30.00"))
                self.assertIsNone(perf.deleted_at)
        finally:
            from django.db import connection

            connection.set_tenant(self.tenant)


class ZeroingImplementationTests(TenantTestCase):
    """Z6 的结构判据：两个触发点**都**经 `zero_performance_amounts`，归零赋值只有一处。"""

    def test_zero_performance_amounts_writes_both_columns(self):
        game = Game.objects.create(name="原神")
        order = Order.objects.create(
            game=game, order_no="20260923000000000801", status=OrderStatus.IN_PROGRESS
        )
        perf = Performance.objects.create(
            order=order,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("66.00"),
            bonus_amount=Decimal("44.00"),
        )
        from apps.business.performance import zero_performance_amounts

        zero_performance_amounts(perf)
        perf.save(update_fields=["temp_amount", "bonus_amount", "updated_at"])
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("0.00"))
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))
