"""价格口径 `3 接发组合` / `4 搜亏本单` 两档的实装口径（2026-10-09 价格口径线 T1 / T2）。

业务方裁定（逐字在 `docs/STATUS.md` 本线条目）：
  - 亏本判据 = **发单价 > 接单价**（严格大于；差额 = 接单价 − 发单价 < 0）；
  - `4 搜亏本单` 档**不使用**金额区间；
  - **不加**「价格必须为正 / 必须已完成 / 必须未退款」之类附加条件 ⇒ 金额 `0` 与负数照常参与。

⚠️ 每条判据都断言**恰好集合**（`sorted()` 比 `order_no` 列表），⛔ **不用 `assertIn`**——
那会漏掉「多回一张」这一类缺陷。
⚠️ 夹具一律**合成值**（E-0077），单号是 20 位数字的**形状**，不是取证记录里的真单号。
⚠️ 列表默认排序是 `-created_at, -id`（`Order.Meta.ordering`）⇒ 一律比 `sorted()`，不钉插入顺序。
"""
import json
from decimal import Decimal

from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order, OrderStatus
from tests.p5_auth_helpers import make_logged_in_client

#: 序列化器 `validate` ③ 的文案（档 `4` + **主栏**区间）——**逐字**抄，⛔ 不用 `assertIn` 判子串。
#: ⚠️ 这条文案按**整句**钉（只判子串会让改写溜过去）。
LOSS_TIER_RANGE_MESSAGE = "「搜亏本单」不使用金额区间，请先清空金额区间。"

#: 序列化器 `validate` ② 的文案（**档 ≠ 3** 却带了发单价区间）——同样**逐字**抄。
HALL_RANGE_WRONG_TIER_MESSAGE = "「发单价区间」只在价格口径的「接发组合」那一档使用，请先清空或改选档位。"

# ── T1 夹具（4 张分档单 + 1 张负价对照）──────────────────────────────────────────
#: 接单价 100 / 发单价 80（**赚**：发 < 接）。
NO_TAKE100_HALL80 = "20261009000000000701"
#: 接单价 100 / 发单价 20（**亏**：发 < 接）。
NO_TAKE100_HALL20 = "20261009000000000702"
#: 接单价 10 / 发单价 80（**亏**：发 > 接）。
NO_TAKE10_HALL80 = "20261009000000000703"
#: 接单价 10 / 发单价 20（**亏**：发 > 接）。
NO_TAKE10_HALL20 = "20261009000000000704"
#: ⚠️ **负价对照单**（接单价 `-5.00` / 发单价 `3.00`）：它必须**任何** T1 区间都筛不到
#: ⇒ 若某条区间条件被静默丢掉（例如把 `is not None` 写成真值、或整块不生效），它会漏进结果集。
NO_NEG_CONTROL = "20261009000000000705"

# ── T2 夹具（业务方口径的六个钉子）──────────────────────────────────────────────
#: 赚：接 100 / 发 80 ⇒ `80 > 100` 不成立。
NO_PROFIT = "20261009000000000711"
#: 平：接 50 / 发 50 ⇒ 严格大于不成立（**这一张是 `gt` 与 `gte` 的分水岭**）。
NO_EVEN = "20261009000000000712"
#: 亏：接 0 / 发 30 ⇒ `30 > 0` 成立（金额 0 照常参与）。
NO_LOSS_ZERO_TAKE = "20261009000000000713"
#: 负亏：接 −100 / 发 −50 ⇒ `-50 > -100` 成立。
NO_LOSS_NEGATIVE = "20261009000000000714"
#: 负不亏：接 0 / 发 −200 ⇒ `-200 > 0` 不成立。
NO_NOT_LOSS_NEGATIVE_HALL = "20261009000000000715"
#: 无发单价：`hall_price IS NULL` ⇒ **不算**亏本（它没有发单价）。
NO_NO_HALL_PRICE = "20261009000000000716"


class _PriceFilterFixture(TenantTestCase):
    """两档共用的底座：一个游戏 + 一个能看到列表的登录态。"""

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        # ⚠️ `make_logged_in_client` 返回的是 **client**（不是 user）。
        self.client = make_logged_in_client(groups=("客服",), nodes=("order.list",))

    def _order(self, order_no, total_price, hall_price, **extra):
        """建一张单。`hall_price=None` ⇒ 列写 NULL（`NO_NO_HALL_PRICE` 那一支）。"""
        return Order.objects.create(
            game=self.game,
            order_no=order_no,
            order_type="BOOST",
            total_price=total_price,
            hall_price=hall_price,
            refund_fee=Decimal("0.00"),
            status=OrderStatus.IN_PROGRESS,
            **extra,
        )

    def _nos(self, query=""):
        body = self.client.get(f"/api/business/orders/{query}").json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        return sorted(r["order_no"] for r in body["data"]["items"])


class PriceComboTests(_PriceFilterFixture):
    """T1：`3 接发组合` 档 —— 接单价与发单价**各自**落在自己的区间里。"""

    def setUp(self):
        super().setUp()
        self._order(NO_TAKE100_HALL80, Decimal("100.00"), Decimal("80.00"))
        self._order(NO_TAKE100_HALL20, Decimal("100.00"), Decimal("20.00"))
        self._order(NO_TAKE10_HALL80, Decimal("10.00"), Decimal("80.00"))
        self._order(NO_TAKE10_HALL20, Decimal("10.00"), Decimal("20.00"))
        self._order(NO_NEG_CONTROL, Decimal("-5.00"), Decimal("3.00"))

    def test_both_ranges_apply_and_the_result_is_exactly_one_row(self):
        """① 两对区间同时生效 ⇒ **恰好** `(100, 80)` 那**一张**。

        ⚠️ 判据用**恰好集合**：只回 0 张（条件接错）与回 2 张以上（少应用一侧）都会红。
        """
        self.assertEqual(
            self._nos(
                "?show_price_type=3&price_min=50&price_max=150"
                "&hall_price_min=50&hall_price_max=150"
            ),
            [NO_TAKE100_HALL80],
        )

    def test_taker_range_alone_returns_the_two_rows_with_taker_price_100(self):
        """② 只给接单价区间 ⇒ 接单价 = 100 的**两张**都回（发单价各不相同，说明发单价没被顺带筛）。"""
        self.assertEqual(
            self._nos("?show_price_type=3&price_min=50&price_max=150"),
            sorted([NO_TAKE100_HALL80, NO_TAKE100_HALL20]),
        )

    def test_hall_range_alone_returns_the_two_rows_with_hall_price_80(self):
        """② 对称的另一半：只给发单价区间 ⇒ 发单价 = 80 的两张都回。"""
        self.assertEqual(
            self._nos("?show_price_type=3&hall_price_min=50&hall_price_max=150"),
            sorted([NO_TAKE100_HALL80, NO_TAKE10_HALL80]),
        )

    def test_bounds_are_inclusive_on_both_sides(self):
        """③ 下界/上界**恰好等于**某单的值 ⇒ 该单**在**（`__gte` / `__lte`，不是严格不等）。

        ⚠️ 两半都要有：只钉下界会让「上界写成 `__lt`」这类改动逃掉。
        """
        # 下界 = 100：接单价**恰好** 100 的两张都在（`__gte`）。
        self.assertEqual(
            self._nos("?show_price_type=3&price_min=100"),
            sorted([NO_TAKE100_HALL80, NO_TAKE100_HALL20]),
        )
        # 上界 = 20：发单价**恰好** 20 的两张都在（`__lte`）。
        # ⚠️ 负价对照单（发单价 `3.00`）**也在**这一组里——它 3 ≤ 20，本来就该命中；
        #    本条的判别力落在「恰好 20 的那两张在不在」，对照单只是顺带被列出来。
        self.assertEqual(
            self._nos("?show_price_type=3&hall_price_max=20"),
            sorted([NO_TAKE100_HALL20, NO_TAKE10_HALL20, NO_NEG_CONTROL]),
        )

    def test_the_main_bar_range_is_the_taker_price_in_tier_3(self):
        """④ 主栏那对区间（`price_min` / `price_max`）在 `3` 档比的是**接单价**。

        判别力：`NO_TAKE100_HALL20` 的发单价是 **20**（小于下界 50）——若 `price_min` 被接到
        `hall_price` 上，它会被排除 ⇒ 本条红。`NO_TAKE10_HALL80` 则必须**不在**（接单价 10 < 50）。
        """
        nos = self._nos("?show_price_type=3&price_min=50")
        self.assertIn(NO_TAKE100_HALL20, nos)
        self.assertNotIn(NO_TAKE10_HALL80, nos)

    def test_the_negative_price_control_never_leaks_in(self):
        """负价对照单：接单价 `-5.00` 低于任何用到的下界 ⇒ 每一组区间都筛不到它。

        ⚠️ 这条不是重复上面几条：上面几条用的是「恰好集合」，本条单独把**对照单**点出来
        ——条件是「整块区间没生效」时它会**同时**漏进多组结果，这一行的失败信息最直白。
        """
        for query in (
            "?show_price_type=3&price_min=50",
            "?show_price_type=3&hall_price_min=50",
            "?show_price_type=3&price_min=50&price_max=150"
            "&hall_price_min=50&hall_price_max=150",
        ):
            with self.subTest(query=query):
                self.assertNotIn(NO_NEG_CONTROL, self._nos(query))


class LossPriceTests(_PriceFilterFixture):
    """T2：`4 搜亏本单` 档 —— 判据 = **发单价 > 接单价**（严格大于），不使用金额区间。"""

    def setUp(self):
        super().setUp()
        self._order(NO_PROFIT, Decimal("100.00"), Decimal("80.00"))
        self._order(NO_EVEN, Decimal("50.00"), Decimal("50.00"))
        self._order(NO_LOSS_ZERO_TAKE, Decimal("0.00"), Decimal("30.00"))
        self._order(NO_LOSS_NEGATIVE, Decimal("-100.00"), Decimal("-50.00"))
        self._order(NO_NOT_LOSS_NEGATIVE_HALL, Decimal("0.00"), Decimal("-200.00"))
        self._order(NO_NO_HALL_PRICE, Decimal("40.00"), None)

    def test_tier_4_returns_exactly_the_two_loss_orders(self):
        """① **恰好**两张：`亏`（接 0 / 发 30）与 `负亏`（接 −100 / 发 −50）。

        ⚠️ 含「金额 0 与负数照常参与」这一半：若实现里加了「价格必须为正」之类条件，
        这两张会被一起筛掉 ⇒ 本行红。
        """
        self.assertEqual(
            self._nos("?show_price_type=4"),
            sorted([NO_LOSS_ZERO_TAKE, NO_LOSS_NEGATIVE]),
        )

    def test_equal_prices_are_not_a_loss(self):
        """② `平`（接 50 / 发 50）**不在** —— `hall_price == total_price` 不是亏本（严格 `>`）。

        判别力：把 `hall_price__gt` 改成 `hall_price__gte` ⇒ 这一行立刻红。
        """
        self.assertNotIn(NO_EVEN, self._nos("?show_price_type=4"))

    def test_null_hall_price_is_not_a_loss(self):
        """③ `无发单价`（`hall_price IS NULL`）**不在**——它没有发单价可比。"""
        self.assertNotIn(NO_NO_HALL_PRICE, self._nos("?show_price_type=4"))

    def test_the_profit_order_is_not_a_loss(self):
        """反面那一半：`赚`（接 100 / 发 80）**不在**（判据写反成「发 < 接」时本行会红）。"""
        self.assertNotIn(NO_PROFIT, self._nos("?show_price_type=4"))

    def test_the_order_decided_by_business_is_the_one_in_their_example(self):
        """业务方原话那条例子（发 200 / 接 0）**算亏本**——夹具里有同形的 `接 0 / 发 30`。

        ⚠️ 本条**故意重复** ① 的一半：它钉的是**方向**（发单价高 = 亏），与上面几条的措辞不同，
        读的人从用例名就能看出「这条对应哪句裁定」。⛔ 不是判据强度上的重复。
        """
        self.assertIn(NO_LOSS_ZERO_TAKE, self._nos("?show_price_type=4"))

    def test_any_amount_range_with_tier_4_is_loudly_rejected(self):
        """④ 档 `4` + **主栏**区间参数 ⇒ `code 1000` + **逐字**文案、且**一行都不出**。

        ⛔ **不许收下再静默丢掉**（E-0021 同族）：那会让客户端以为「按我给的区间 + 亏本」筛，
        实际拿到另一批数据。两个参数**逐个**给一遍（任一漏判都会在这里显形）。

        ⚠️ **修复轮改准**：本用例原先对 4 个参数一律断言
        `assertIsNone(body["data"])` —— 那是**恒真**判据（失败信封的 `data` 恒为 `None`，
        E-0063 同族：恒真的判据在测试里只是没覆盖），而且 `assertIn` 只判子串。现在：
          - **阳性对照**在前：**不带**区间时这一档**查得到**那两张亏本单（证明「1000」不是因为
            夹具里本来就没单）；
          - 文案**逐字**断言（`assertEqual`，⛔ 不是 `assertIn`）；
          - 「一行都不出」改成**有判别力**的判据：整个响应体里**不出现**任何夹具单号
            （若哪天实现改成「收下 + 顺便回一批数据」，本行会红）。
        ⚠️ 本支只覆盖**主栏那一对**：`hall_price_min` / `hall_price_max` 在档 `4` 上由
        `HallPriceRangeTierGateTests` 那一组钉住（那条文案更具体，见序列化器 `validate` ②）。
        """
        # 阳性对照（判据的前提）：这一档**不带**区间时确实查得到那两张亏本单。
        self.assertEqual(
            self._nos("?show_price_type=4"),
            sorted([NO_LOSS_ZERO_TAKE, NO_LOSS_NEGATIVE]),
        )
        for key in ("price_min", "price_max"):
            with self.subTest(param=key):
                body = self.client.get(
                    f"/api/business/orders/?show_price_type=4&{key}=10"
                ).json()
                self.assertEqual(body["code"], 1000)
                self.assertEqual(body["msg"], LOSS_TIER_RANGE_MESSAGE)
                self.assertNotIn(NO_LOSS_ZERO_TAKE, json.dumps(body, ensure_ascii=False))
                self.assertNotIn(NO_LOSS_NEGATIVE, json.dumps(body, ensure_ascii=False))

    def test_the_zero_bound_counts_as_given_not_as_missing(self):
        """⛔ `0` 是**给了这个条件**、不是「没给」（E-0021）：档 `4` + `price_min=0` 也必须被拒。

        ⚠️ 与上一条**故意分成两条**：上一条逐个参数名给**非零**值（覆盖面），本条给**边界值**
        ——若互斥判据写成真值判断（`if attrs.get("price_min"):`），`0` 会被当成「没给」、
        这条会以 `code 0` 红，而上一条照样绿。两条各钉一半，⛔ 不要合并。
        负数同理（`price_min=-1` 是真值）⇒ 这里不需要再列一遍。
        """
        body = self.client.get("/api/business/orders/?show_price_type=4&price_min=0").json()
        self.assertEqual(body["code"], 1000)
        self.assertEqual(body["msg"], LOSS_TIER_RANGE_MESSAGE)
        self.assertNotIn(NO_LOSS_ZERO_TAKE, json.dumps(body, ensure_ascii=False))


class HallPriceRangeTierGateTests(_PriceFilterFixture):
    """发单价那一对区间**只在档 `3` 有效**（2026-10-09 修复轮补的洞，E-0021 同族）。

    缺陷形状：`selectors.search_orders` 只在 `3` 档读 `hall_price_min` / `hall_price_max`
    ⇒ 档 `0/1/2`（**含缺省**，落 `default="0"`）或档 `4` 带上它们会被**默默丢掉**，
    而客户端以为自己在「按发单价区间」筛（拿到的是另一批数据）。
    """

    def setUp(self):
        super().setUp()
        self._order(NO_TAKE100_HALL80, Decimal("100.00"), Decimal("80.00"))
        self._order(NO_TAKE100_HALL20, Decimal("100.00"), Decimal("20.00"))

    def test_every_other_tier_rejects_the_hall_range_with_the_verbatim_message(self):
        """① 档 `0/1/2/4`（**逐个**）+ `hall_price_min=0` ⇒ `1000` + **逐字**文案。

        ⚠️ 四个档**逐个**给：只钉一个档会让「闸写成 `== "0"`」这类改动逃掉。
        ⚠️ 值用 `0`（falsy）：判据若写成真值判断，本行会以 `code 0` 红（E-0021）。
        ⚠️ **缺省档**（完全不传 `show_price_type`）也一并钉住：它落 `default="0"`，
        与显式 `=0` 走同一条路，但那是个**独立的**请求形状。
        """
        cases = ("0", "1", "2", "4", "")
        for tier in cases:
            with self.subTest(show_price_type=tier or "(缺省)"):
                prefix = f"?show_price_type={tier}&" if tier else "?"
                body = self.client.get(
                    f"/api/business/orders/{prefix}hall_price_min=0"
                ).json()
                self.assertEqual(body["code"], 1000)
                self.assertEqual(body["msg"], HALL_RANGE_WRONG_TIER_MESSAGE)

    def test_the_upper_bound_is_rejected_too(self):
        """① 的另一半：`hall_price_max` **单独**给也要被拒（只钉下界会让上界漏掉）。"""
        body = self.client.get(
            "/api/business/orders/?show_price_type=1&hall_price_max=20"
        ).json()
        self.assertEqual(body["code"], 1000)
        self.assertEqual(body["msg"], HALL_RANGE_WRONG_TIER_MESSAGE)

    def test_tier_3_still_accepts_both_hall_bounds(self):
        """② **对照**（防「一刀切拒」）：档 `3` + 两个 hall 区间 ⇒ 正常（`code 0`）且真的生效。

        ⚠️ 这一条是那条闸的**反面**：没有它，把 `!= "3"` 写成 `!= "3" or True`（即一律拒）
        也会让上面两条绿。
        """
        self.assertEqual(
            self._nos("?show_price_type=3&hall_price_min=50&hall_price_max=150"),
            [NO_TAKE100_HALL80],
        )
        self.assertEqual(
            self._nos("?show_price_type=3&hall_price_min=0&hall_price_max=25"),
            [NO_TAKE100_HALL20],
        )

    def test_tier_4_with_the_main_bar_range_keeps_its_own_message(self):
        """③ 档 `4` + `price_min` ⇒ **仍是**「搜亏本单」那条文案（逐字）。

        ⚠️ 这条钉的是**两条闸的先后**：档 `4` + **发单价**区间同时命中两条闸时，给用户的诊断是
        「这一档不该有发单价区间」（更具体，见上一条用例组），而**主栏**区间仍走原来那条
        ⇒ 顺序换了（两条闸对调）本行会红。
        """
        body = self.client.get(
            "/api/business/orders/?show_price_type=4&price_min=10"
        ).json()
        self.assertEqual(body["code"], 1000)
        self.assertEqual(body["msg"], LOSS_TIER_RANGE_MESSAGE)

    def test_tier_3_with_only_the_main_bar_range_is_untouched(self):
        """档 `3` + **只有**主栏区间（不带任何 hall 参数）⇒ 正常（那条闸不许误伤它）。"""
        self.assertEqual(
            self._nos("?show_price_type=3&price_min=50"),
            sorted([NO_TAKE100_HALL80, NO_TAKE100_HALL20]),
        )
