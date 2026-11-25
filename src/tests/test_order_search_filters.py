"""订单列表新增筛选的口径（spec §5.4 / §8-T9 第 45/46 条；D6 的命名规则 / D11 的逐条处置）。

⚠️ 每条筛选都断言**两半**（命中 / 不命中），并用**未删的对照单**防「一律空」的假通过。
⚠️ 金额边界单列（第 46 条）：`price_min=0` / `price_max=0` **必须真的被应用**（E-0021）；
   负价对照单 `NO_NEG`（`total_price=-5.00`）让「单侧把 `is not None` 换成真值」也能被抓到。
⚠️ 夹具值一律合成。
⚠️ 列表默认排序是 `-created_at, -id`（`Order.Meta.ordering`）⇒ 断言一律用 `sorted()`
   比较集合，**不钉插入顺序**（计划里的示例按插入顺序写，那是错的口径）。
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderRefund,
    OrderSource,
    OrderSourceGroup,
    OrderStatus,
    Performance,
)
from tests.p5_auth_helpers import make_logged_in_client

NO_SRC = "20260924000000000601"
NO_OTHER = "20260924000000000602"
NO_ZERO = "20260924000000000603"
#: 负价**对照单**（`total_price=-5.00`）：`price_min=0` 被静默丢掉时它会漏进结果集
#: ⇒ 让「`is not None` 换成真值」这个单侧变异在金额用例上**变红**（E-0021）。
NO_NEG = "20260924000000000604"


class _SearchFixture(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.source = OrderSource.objects.create(name="测试来源甲")
        self.other_source = OrderSource.objects.create(name="测试来源乙")
        self.order = Order.objects.create(
            game=self.game, order_no=NO_SRC, order_source=self.source,
            order_type="BOOST", total_price=Decimal("100.00"),
            hall_price=Decimal("80.00"), refund_fee=Decimal("10.00"),
            status=OrderStatus.IN_PROGRESS, created_by="测试录单人甲",
        )
        self.other = Order.objects.create(
            game=self.game, order_no=NO_OTHER, order_source=self.other_source,
            order_type="COMPANION", total_price=Decimal("50.00"),
            hall_price=Decimal("50.00"), refund_fee=Decimal("0.00"),
            status=OrderStatus.COMPLETED,
        )
        self.zero = Order.objects.create(
            game=self.game, order_no=NO_ZERO, order_source=self.source,
            total_price=Decimal("0.00"), hall_price=Decimal("30.00"),
            refund_fee=Decimal("0.00"), status=OrderStatus.UNASSIGNED,
        )
        # ⚠️ `make_logged_in_client` 返回的是 **client**（不是 user）⇒ 要拿 user 对象
        #    得按用户名回查（计划示例里的 `.user` 不存在，已按真实签名改）。
        make_logged_in_client(username="测试客服甲")
        self.sales = get_user_model().objects.get(username="测试客服甲")
        self.order.sales_user = self.sales
        # ⚠️ `created_by` 是**文本列**（录入时的用户名快照）⇒ 夹具把它写成**同一个人的用户名**，
        #    这样 `kefu_type=5` 那一档才与 `kefu_type=0` 命中同一张单（可比对）。
        self.order.created_by = self.sales.get_username()
        self.order.save(update_fields=["sales_user", "created_by"])
        # ⚠️ `nodes=` 必须配 `groups=`：`_grant_nodes` 只把节点挂到「当前所在 Group 对应的
        #    Role」上，没有组 ⇒ 节点一个都挂不上 ⇒ 403（夹具语义见 `p5_auth_helpers` docstring）。
        self.client = make_logged_in_client(groups=("客服",), nodes=("order.list",))

    def _nos(self, query=""):
        body = self.client.get(f"/api/business/orders/{query}").json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        return [r["order_no"] for r in body["data"]["items"]]


class OrderSourceAndTypeTests(_SearchFixture):
    def test_order_source_filters_by_the_dictionary_foreign_key(self):
        self.assertEqual(
            sorted(self._nos(f"?order_source={self.source.pk}")),
            sorted([NO_SRC, NO_ZERO]),
        )
        self.assertEqual(self._nos(f"?order_source={self.other_source.pk}"), [NO_OTHER])

    def test_order_type_uses_the_shared_enum_codes(self):
        self.assertEqual(self._nos("?order_type=BOOST"), [NO_SRC])
        self.assertEqual(self._nos("?order_type=COMPANION"), [NO_OTHER])

    def test_bad_order_type_is_1000_not_silently_ignored(self):
        self.assertEqual(self.client.get("/api/business/orders/?order_type=0").json()["code"], 1000)


class SalesUserAndKefuTypeTests(_SearchFixture):
    def test_sales_user_filters_by_the_foreign_key(self):
        self.assertEqual(self._nos(f"?sales_user={self.sales.pk}"), [NO_SRC])

    def test_kefu_type_0_is_the_foreign_key_branch(self):
        """`0` 档 ⇒ `sales_user_id`（与 `sales_user` 单独用**同一结果**）。"""
        self.assertEqual(self._nos(f"?kefu_type=0&sales_user={self.sales.pk}"), [NO_SRC])

    def test_kefu_type_5_matches_the_created_by_text_column(self):
        """`5` 档 ⇒ `created_by`（**文本列**，把请求里的 id 解析成用户名再等值匹配）。

        ⚠️ 降级如实登记（§3-D11 半可做 ②）：账号改名后按新名筛**筛不到旧单**。
        """
        # 夹具里 `order.created_by` 就是 `self.sales.get_username()`（见 `setUp`）。
        self.assertEqual(self._nos(f"?kefu_type=5&sales_user={self.sales.pk}"), [NO_SRC])

    def test_kefu_type_alone_does_not_filter_anything(self):
        """⛔ **不许静默按某个默认人筛**：只给档、不给值 ⇒ 结果集与不传任何参数**相同**。"""
        self.assertEqual(sorted(self._nos("?kefu_type=5")), sorted(self._nos("")))

    def test_kefu_type_10_matches_the_refund_operator_text_column_without_row_fanout(self):
        """`10` 档 ⇒ `refunds__operator`（**文本列**）+ `.distinct()`。

        ⚠️ `refunds` 是**多值关系** ⇒ 不 `distinct()` 会把同一单放大成多行。夹具给该单
        **两条**退款记录（同一操作人），断言结果**恰好一行**。
        """
        for index, amount in enumerate((Decimal("3.00"), Decimal("4.00"))):
            OrderRefund.objects.create(
                order=self.order,
                kind=OrderRefund.Kind.PARTIAL,
                amount=amount,
                reason="测试退款",
                operator=self.sales.get_username(),
                # `request_id` 上有具名唯一约束 ⇒ 两条必须给不同的值。
                request_id=f"test-filter-refund-{index}",
            )
        nos = self._nos(f"?kefu_type=10&sales_user={self.sales.pk}")
        self.assertEqual(nos, [NO_SRC])          # ⛔ 不是 [NO_SRC, NO_SRC]（join 被 distinct 收住）

    def test_kefu_type_10_does_not_inflate_the_status_counts(self):
        """⭐ `.distinct()` 收住的是**行集**，收不住 `GROUP BY` 的计数（M1 的缺陷）。

        夹具：`self.other`（`COMPLETED`、`total_price=50.00`）挂 **3 条**操作人相同的退款记录
        ⇒ `kefu_type=10` 的 JOIN 把这一单放成 3 行。判据：

        - 筛选响应里 `status_counts["COMPLETED"]` 必须是 **1**（非 distinct 的 `Count("id")`
          会数到 **3**——这正是本条要抓的红态）；
        - **对照半**（不筛选）：没有那条 JOIN ⇒ 同样是 1，证明「3」只可能来自 JOIN 放大；
        - 同一响应的 `count` / `total_price` 不放大（`.count()` 与 `Sum` 走 Django 的
          distinct 子查询路径）——它们把缺陷**定位**到 GROUP BY，而不是「整个聚合都错了」。
        """
        for index, amount in enumerate(
            (Decimal("3.00"), Decimal("4.00"), Decimal("5.00"))
        ):
            OrderRefund.objects.create(
                order=self.other,
                kind=OrderRefund.Kind.PARTIAL,
                amount=amount,
                reason="测试退款丁",
                operator=self.sales.get_username(),
                request_id=f"test-summary-refund-{index}",
            )
        body = self.client.get(
            f"/api/business/orders/?kefu_type=10&sales_user={self.sales.pk}"
        ).json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        # 非空哨兵：筛选确实命中了那一张单（否则下面的「= 1」会因空集假通过）。
        self.assertEqual([r["order_no"] for r in body["data"]["items"]], [NO_OTHER])
        summary = body["data"]["summary"]
        self.assertEqual(summary["status_counts"]["COMPLETED"], 1)
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["total_price"], "50.00")
        # 对照半：**不带** `kefu_type` ⇒ 没有 JOIN ⇒ 同一个状态计数也是 1。
        control = self.client.get("/api/business/orders/").json()["data"]["summary"]
        self.assertEqual(control["status_counts"]["COMPLETED"], 1)


class LeakAndPriceTests(_SearchFixture):
    def _add_negative_price_order(self):
        """现场造一张负价**对照单**（只在金额那两条里用）。

        ⚠️ 不放进 `_SearchFixture.setUp`：`is_leak` 那条的期望值是按现有三张单写死的
        （`[NO_OTHER]`），多一张常驻单会改它的集合——那不是本任务要动的东西。
        """
        return Order.objects.create(
            game=self.game, order_no=NO_NEG,
            total_price=Decimal("-5.00"), hall_price=Decimal("-5.00"),
            refund_fee=Decimal("0.00"), status=OrderStatus.UNASSIGNED,
        )

    def test_is_leak_matches_unassigned_or_without_a_main_taker(self):
        # `NO_ZERO` 是未分配 ⇒ 命中；`NO_SRC` 没有 MAIN 绩效行 ⇒ 也命中；`NO_OTHER` 有 MAIN 行 ⇒ 不命中。
        Performance.objects.create(
            order=self.other, emp_type=EmpType.MAIN, user=self.sales,
            temp_amount=Decimal("5.00"),
        )
        self.assertEqual(sorted(self._nos("?is_leak=true")), sorted([NO_SRC, NO_ZERO]))
        self.assertEqual(self._nos("?is_leak=false"), [NO_OTHER])

    def test_price_zero_bounds_are_really_applied(self):
        """第 46 条（E-0021）：`0` 是**携带了条件**，不是「没传」。

        ⭐ 负价对照单 `NO_NEG`（`total_price=-5.00`）是这条**单侧**判别力的来源：
        只把 `if price_min is not None:` 换成 `if price_min:` ⇒ 下界 `0` 被静默丢掉、
        `price_max=0` 仍在 ⇒ `-5.00 <= 0` ⇒ `NO_NEG` 会漏进结果 ⇒ 本行红。
        """
        self._add_negative_price_order()
        nos = self._nos("?show_price_type=0&price_min=0&price_max=0")
        self.assertIn(NO_ZERO, nos)
        self.assertNotIn(NO_SRC, nos)
        self.assertNotIn(NO_NEG, nos)     # 丢掉 `price_min=0`（单侧变异）⇒ 这一行红

    def test_a_negative_bound_is_legal_and_is_really_applied(self):
        """D-0015 / E-0021：`price_min` 为**负数合法**，而且必须**真的被应用**。

        判据拆两半：① `code == 0`（合法，不被序列化器拒成 `1000`）；② 用负价对照单
        证明这个下界进了查询——`price_min=-3` ⇒ `-5.00` 那单被挡在外面、`0.00` 那单在。
        ⚠️ 负数本身是 Truthy ⇒ 本行**抓不到**「`is not None` 换成真值」那个变异，
        抓它的是 `test_price_zero_bounds_are_really_applied`（`0` 才是 falsy）。
        """
        self._add_negative_price_order()
        body = self.client.get("/api/business/orders/?show_price_type=0&price_min=-3").json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        nos = [r["order_no"] for r in body["data"]["items"]]
        self.assertIn(NO_ZERO, nos)       # 0.00 >= -3 ⇒ 在
        self.assertNotIn(NO_NEG, nos)     # -5.00 < -3 ⇒ 不在（下界真的被应用）

    def test_show_price_type_picks_the_matching_column(self):
        # `NO_ZERO` 的 total_price=0 / hall_price=30；按口径 1（发单价）下界 20 ⇒ 命中；
        # 按口径 0（接单价）下界 20 ⇒ 不命中。
        self.assertIn(NO_ZERO, self._nos("?show_price_type=1&price_min=20"))
        self.assertNotIn(NO_ZERO, self._nos("?show_price_type=0&price_min=20"))

    def test_price_type_closed_set_is_0_to_4_and_out_of_range_is_rejected(self):
        """取值是**闭集 `0..4`**：`3` / `4` **被接受**（2026-10-09 实装），越界值仍 `1000`。

        ⚠️ **本条改准（价格口径线 T3）**：原用例名 `test_unsupported_price_types_are_rejected`，
        它断言 `for value in ("3","4")` ⇒ `code == 1000`。两档实装之后那个事实变了
        ⇒ 按事实改准，⛔ **不是删掉、也不是放宽成 `toContain` 那种松判据**：
          - `3` / `4` 两半从「拒收」翻成「**接受**（`code == 0`）」；
          - 「闭集」这件事改由**越界值**（`5`、`6`、`-1`）继续钉住——断言强度**没有降低**：
            原判据只覆盖 `3/4` 两个具体值，现在覆盖两档接受 **＋** 三个越界值拒绝。
        ⚠️ `5` 是**紧邻上界**的那个值（不是随手挑的大数）：`choices` 若被误写成 `0..5`，
        本条会在 `5` 这一格红。
        ⚠️ **空串不进这一组**：`order_search_params` 把空串当「没传」丢掉（那条策略的例外清单
        `BLANK_IS_A_VALUE` 里只有 `payment_status`）⇒ `?show_price_type=` 走默认档 `0`、
        回 `code 0`。把它写进「越界值」会把一条**假判据**钉进本网。
        """
        for value in ("3", "4"):
            with self.subTest(accepted=value):
                self.assertEqual(
                    self.client.get(
                        f"/api/business/orders/?show_price_type={value}"
                    ).json()["code"],
                    0,
                )
        for value in ("5", "6", "-1"):
            with self.subTest(rejected=value):
                self.assertEqual(
                    self.client.get(
                        f"/api/business/orders/?show_price_type={value}"
                    ).json()["code"],
                    1000,
                )


class BatchOrderNumbersTests(_SearchFixture):
    def test_order_nos_accepts_a_comma_separated_list_of_20_digit_numbers(self):
        self.assertEqual(
            sorted(self._nos(f"?order_nos={NO_SRC},{NO_OTHER}")), sorted([NO_SRC, NO_OTHER])
        )

    def test_order_nos_accepts_mixed_system_and_source_numbers(self):
        order = Order.objects.get(order_no=NO_SRC)
        for source in ("SRC-TEST-1", "1" * 19, "2" * 20):
            with self.subTest(source=source):
                order.source_order_no = source
                order.save(update_fields=["source_order_no"])
                self.assertEqual(self._nos(f"?batch_lookup_nos={source}"), [NO_SRC])
                unknown = "x" * 129
                self.assertCountEqual(
                    self._nos(f"?batch_lookup_nos={NO_OTHER},{source},{unknown}"),
                    [NO_SRC, NO_OTHER],
                )
                self.assertEqual(self._nos(f"?batch_lookup_nos={unknown}"), [])
                if len(source) == 20:
                    self.assertEqual(self._nos(f"?order_nos={source}"), [])

    def test_malformed_order_nos_is_1000(self):
        for bad in (f"{NO_SRC},", f",{NO_SRC}"):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.client.get(f"/api/business/orders/?batch_lookup_nos={bad}").json()["code"], 1000
                )

    def test_order_nos_over_200_tokens_is_1000(self):
        """个数上界（`validate_order_nos`：≤200 个 token）——201 个**各自合法**的 token 也当场 `1000`。

        ⚠️ 评审 L1：旧 RegexField 口径下这个闸靠 `max_length` 顺手挡；
        D-0301 放宽成逐 token 校验后它是**独立分支**，必须有自己的判据。
        """
        tokens = ",".join(f"SRC-TOKEN-{i}" for i in range(201))
        self.assertEqual(
            self.client.get(f"/api/business/orders/?order_nos={tokens}").json()["code"], 1000
        )


# ══════════════════════════════════════════════════════════════════════
# 公司店铺分组（计划 `docs/plans/2026-10-26-order-source-groups.md` §4 #1-#3）
# ══════════════════════════════════════════════════════════════════════
class SourceGroupTests(_SearchFixture):
    """`?source_group=`：只留「其来源属于该分组」的单。

    三半都要断：**命中**（组内来源的单出现）、**不命中**（别组来源的单不出现）、
    **不传 = 与今天逐字相同**（整份夹具都出现）。⚠️ 夹具值一律合成。
    """

    def setUp(self):
        super().setUp()
        self.group = OrderSourceGroup.objects.create(name="测试分组甲")
        # 把「测试来源甲」放进分组；「测试来源乙」保持未分组。
        self.source.group = self.group
        self.source.save(update_fields=["group"])

    def test_hit_and_miss(self):
        self.assertEqual(
            sorted(self._nos(f"?source_group={self.group.pk}")),
            sorted([NO_SRC, NO_ZERO]),
        )
        # 反向：另一组的单不该出现。
        self.assertNotIn(NO_OTHER, self._nos(f"?source_group={self.group.pk}"))

    def test_other_group_returns_only_its_own_order(self):
        other_group = OrderSourceGroup.objects.create(name="测试分组乙")
        self.other_source.group = other_group
        self.other_source.save(update_fields=["group"])
        self.assertEqual(self._nos(f"?source_group={other_group.pk}"), [NO_OTHER])

    def test_absent_param_keeps_todays_result_verbatim(self):
        """不传 = 与今天逐字相同：整份夹具（含未分组的来源乙）都在。"""
        self.assertEqual(
            sorted(self._nos("")),
            sorted([NO_SRC, NO_OTHER, NO_ZERO]),
        )
        # 与「按组筛」对照：组筛的结果**真的是子集**（否则上面那条在空集实现下也会绿）。
        self.assertLess(
            len(self._nos(f"?source_group={self.group.pk}")),
            len(self._nos("")),
        )

    def test_multiple_memberships_match_each_group_without_duplicate_orders(self):
        another_group = OrderSourceGroup.objects.create(name="测试分组乙")
        self.source.groups.add(self.group, another_group)

        for group in (self.group, another_group):
            with self.subTest(group=group):
                body = self.client.get("/api/business/orders/", {"source_group": group.pk}).json()
                self.assertEqual(body["code"], 0)
                self.assertEqual(body["data"]["total"], 2)
                self.assertCountEqual(
                    [row["order_no"] for row in body["data"]["items"]], [NO_SRC, NO_ZERO],
                )

        self.assertCountEqual(self._nos(""), [NO_SRC, NO_OTHER, NO_ZERO])

    def test_deleted_group_no_longer_matches(self):
        """删分组 ⇒ 来源变未分组（SET_NULL）⇒ 用那个（已不存在的）id 筛**回空**、不报错。"""
        group_id = self.group.pk
        self.group.delete()
        body = self.client.get(f"/api/business/orders/?source_group={group_id}").json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["items"], [])

    def test_zero_and_non_integer_are_1000(self):
        for raw in ("0", "-1", "abc"):
            with self.subTest(raw=raw):
                body = self.client.get(f"/api/business/orders/?source_group={raw}").json()
                self.assertEqual(body["code"], 1000, body)
                self.assertIsNone(body["data"])


# ══════════════════════════════════════════════════════════════════════
# 订单状态多选（业务方指令「把这个管理订单页面的多选状态实装起来」）
# ══════════════════════════════════════════════════════════════════════
class StatusMultiSelectTests(_SearchFixture):
    """`?status=` 收**逗号串枚举码**（`IN_PROGRESS,COMPLETED` 这种；对标 `orderSta` 的形状）。

    夹具三张单：`order`=IN_PROGRESS / `other`=COMPLETED / `zero`=UNASSIGNED
    ⇒ 任意两个码的并集都能与第三个区分（筛中 / 不命中两半都断得到）。
    """

    def test_single_value_keeps_working_verbatim(self):
        """单值 = 逗号串的特例：`status__in=[x]` ≡ 旧 `status=x`（既有调用方的行为不变）。"""
        self.assertEqual(self._nos("?status=IN_PROGRESS"), [NO_SRC])
        self.assertEqual(self._nos("?status=COMPLETED"), [NO_OTHER])
        self.assertEqual(self._nos("?status=UNASSIGNED"), [NO_ZERO])

    def test_comma_separated_list_filters_by_the_union(self):
        self.assertEqual(
            sorted(self._nos("?status=IN_PROGRESS,COMPLETED")),
            sorted([NO_SRC, NO_OTHER]),
        )
        # 顺序无关（`__in` 是集合语义）
        self.assertEqual(
            sorted(self._nos("?status=COMPLETED,IN_PROGRESS")),
            sorted([NO_SRC, NO_OTHER]),
        )
        # 不在串里的那半**不出现**（否则「筛了」与「全返回」不可区分）
        self.assertNotIn(NO_ZERO, self._nos("?status=IN_PROGRESS,COMPLETED"))
        # 三个全给 = 整份夹具（与「不传」结果相同，但这是筛选命中、不是静默全量）
        self.assertEqual(
            sorted(self._nos("?status=IN_PROGRESS,COMPLETED,UNASSIGNED")),
            sorted([NO_SRC, NO_OTHER, NO_ZERO]),
        )

    def test_blank_string_is_not_passed_not_a_filter(self):
        """`?status=`（空串）按「没传」处理（`order_search_params` 统一丢空串）⇒ 不筛。"""
        self.assertEqual(
            sorted(self._nos("?status=")),
            sorted([NO_SRC, NO_OTHER, NO_ZERO]),
        )

    def test_bad_token_is_1000_naming_the_token(self):
        """任一非法 token ⇒ `1000`，且 msg **点名**那个 token（⛔ 不静默丢掉那个条件）。"""
        for raw in ("IN_PROGRESS,BOGUS", "BOGUS,IN_PROGRESS", "BOGUS"):
            with self.subTest(raw=raw):
                body = self.client.get(f"/api/business/orders/?status={raw}").json()
                self.assertEqual(body["code"], 1000, body)
                self.assertIsNone(body["data"])
                self.assertIn("BOGUS", body["msg"])

    def test_separator_only_string_is_1000(self):
        """`,,` / 尾随逗号里的**空 token** 是拼错的形状 ⇒ `1000`（与 `order_nos` 的「不能有空项」同判据）。"""
        for raw in (",,", "IN_PROGRESS,", ",IN_PROGRESS"):
            with self.subTest(raw=raw):
                body = self.client.get(f"/api/business/orders/?status={raw}").json()
                self.assertEqual(body["code"], 1000, body)
                self.assertIn("空项", body["msg"])

    def test_over_50_tokens_is_1000(self):
        """超个数（>50）⇒ `1000`，⛔ 不静默截断成前 50 个（与 `username` 那条同族）。"""
        too_many = ",".join("IN_PROGRESS" for _ in range(51))
        body = self.client.get(f"/api/business/orders/?status={too_many}").json()
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("50", body["msg"])
        # 恰好 50 个（合法上界）：`OrderStatus` 只有 8 个码 ⇒ 重复给同一码也算 50 个 token；
        # 这是**入口上限**的边界，不是「有 50 个状态」。
        boundary = ",".join("IN_PROGRESS" for _ in range(50))
        body = self.client.get(f"/api/business/orders/?status={boundary}").json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["items"][0]["order_no"], NO_SRC)

    def test_overlong_string_is_1000(self):
        """整串长度 >1500 ⇒ `1000`（`CharField(max_length=1500)` 的闸，E-0010：不进 `__in` 查询）。"""
        body = self.client.get(f"/api/business/orders/?status={'X' * 1501}").json()
        self.assertEqual(body["code"], 1000, body)
        self.assertIsNone(body["data"])
