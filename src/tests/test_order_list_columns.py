"""管理订单页列表载荷的 10 个只读键（spec §5 的表 / §8 后端第 1–4、8 条）。

10 个键（键名是前端 Task 3 的契约，一个都不能少、不能改名）：
`game_role_name` / `customer_ww` / `hall_price` / `sales_agent` / `created_by` /
`remark` / `net_received` / `hall_publisher_name` / `completed_at` / `time_requirement`。

⚠️ 原先是 9 个（Task 2 落地）：第 10 个 `time_requirement` 是 **Task 3 执行期补的**——
spec §5 的表漏列了它，而 §7.3 要求「时间」列渲染 `time_requirement`（该列是 spec 决策 C5
的刻意偏离）。没有它那一列只能恒空，所以由 Task 3 一起补上（同一个「加法」族）。

## 纪律（为什么这样写）

- **断言打在真出口上**：全部走 `GET /api/business/orders/`（`OrderListSerializer` 是**输出**
  序列化器），断言的是 HTTP 响应体里的值。
- `net_received` 的期望值一律写**字面量**（E-0024）：在测试里重算一遍算式 = 与被测对象同一个
  符号，算式被改坏时不会红。另有 `test_payload_delegates_to_the_shared_function` 一条**同源**
  钉子：把 `settlement_amount.net_received` 换成桩，载荷必须跟着变——它钉的是「载荷真的调了
  那个函数」，不是「两条算式碰巧给出同一个数」。
- 金额边界一律 `is not None`：`0.00` 与负数都是**合法值**（D-0015 / E-0021），
  「没填」与「填了 0」不是同一件事。
- 跨租户的否定断言**不用主键**：两个 schema 的主键序列各自从 1 开始（`AGENTS.md` §5）。

⚠️ **钱包发薪线）**：列表载荷又加了第 11 个只读键 `taker_settled`（「发单价」列的
绿色「结」标记 = 该单**打手**绩效是否已结账）。它**不并进 `TEN_KEYS`**，单列 `SETTLE_KEYS`
——同 `PAYSTATUS_KEYS` / `SPLIT_KEYS` 的先例（每批一张清单；混进同一个元组会让各批互相污染）。
⛔ 但它与前两批不同：它加在**基类** `OrderListSerializer.Meta.fields` 上 ⇒ 大厅 / 回收站 /
待归属也带这个键（值恒 `False`），那两处的**恰好集合**网已同批登记。

⭐ **来源字典当前名）**：列表载荷再加一个只读键 `source_name`（「订单来源」列的
显示值 = 挂字典时字典**当前名**、否则 `source` 快照），单列 `SOURCE_NAME_KEYS`。
与 `SPLIT_KEYS` 同一处置：**只**加在 `OrderSplitListSerializer`（订单列表这一条读面），
大厅 / 回收站 / 待归属**不带**它 ⇒ 那两处的恰好集合网这一批一个字没动。
"""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.dailiantong_push import placeholder_taker_user
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    OrderType,
    Performance,
    SettleStatus,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_games, make_logged_in_client

LIST_URL = "/api/business/orders/"
HALL_URL = "/api/business/hall/"
#: 财务「待归属」清单（`apps/business/urls.py`；**没有**尾斜杠）。它也用 `OrderListSerializer`
#: ⇒ `hall_publisher_name` 的 FK 解引用在**第三个**消费方上同样暴露（F4）。
UNATTRIBUTED_URL = "/api/business/settlements/unattributed"

#: 列表端点的类级节点门（`OrderViewSet.ORDER_PERMISSIONS["list"]`）——不挂它拿 403。
LIST_NODES = ("order.list",)
#: 大厅的节点门。
HALL_NODES = ("grab_hall.take_order",)
#: 发单价的读取脱敏 flag。
OFFER_FLAG = "order.restrict_view_offer_price"

#: 列表载荷承诺的 10 个键（**一个不能少**：缺任何一个都由本文件第一条用例指出）。
#: ⚠️ 判据形状是「缺键集合为空」，**不是**「恰好这 10 个」——载荷本来就带既有键
#: （`order_no` 等），`spec` 只承诺这些是**加法**；这条区别下面那条用例自己写明了。
TEN_KEYS = (
    "game_role_name",
    "customer_ww",
    "hall_price",
    "sales_agent",
    "created_by",
    "remark",
    "net_received",
    "hall_publisher_name",
    "completed_at",
    # 第 10 个（spec §5 原表漏列，而 §7.3 的「时间」列要读它）。
    "time_requirement",
)

#: 本批（价格与收款状态）新增的两个键（spec §5.3 / §8 第 16 条）。
#: 与 `TEN_KEYS` **分开命名**：那 10 个是「列」第二批的契约，这两个是本批的加法；
#: 混进同一个元组会让两批的清单互相污染，也让任何一批改键时两批一起红。
PAYSTATUS_KEYS = ("payment_status", "payment_method")

#: ⭐ **（拆单）**：订单列表载荷**末尾**追加的两个键
#: （`OrderSplitListSerializer`，**只**加在订单列表这一条读面上）。
#: - `split_id`：空串 = 不是拆单；非空 = 拆单组 ID（组内共用）。
#: - `split_members`：int，**组内成员总数**（非拆单 = `0`）。
#: ⛔ 大厅 / 回收站 / 财务「待归属」三处**刻意不加**（它们各有**恰好集合**的键表钉子，
#: 见 `OrderSplitListSerializer` 的 docstring）——本文件的
#: `SplitKeysAreOrderListOnlyTests` 也把这条偏离钉住。
SPLIT_KEYS = ("split_kind", "split_id", "split_members")

#: ⭐ **D-0357（G16）**：订单列表载荷新增的两键
#: （`OrderSplitListSerializer`，与 `SPLIT_KEYS` 同一处置——**只**加在订单列表）。
#: - `split_completed_members`：int，组内已完成数（非拆单 = `0`）；
#: - `split_is_main`：bool，本行是否组内 `id` 最小者（非拆单**恒 `False`**）。
SPLIT_V2_KEYS = (
    "split_completed_members", "split_is_main", "split_is_balance_anchor",
)

#: ⭐ **钱包发薪线**：订单列表载荷新增的 `taker_settled`
#: （该单**打手绩效**（`Performance.emp_type=MAIN`）是否已结账；「发单价」列的绿色「结」标记）。
#: 与 `TEN_KEYS` / `PAYSTATUS_KEYS` / `SPLIT_KEYS` **分开命名**（同那两批的理由：
#: 混进同一个元组会让各批的清单互相污染）。⛔ 与前两批不同，它**在基类**
#: `OrderListSerializer.Meta.fields` 上 ⇒ 大厅 / 回收站 / 待归属也跟着多这个键（值恒 `False`）；
#: 那两处的**恰好集合**网已同批登记（`RECYCLE_ROW_KEYS` / `REMOTE_ROW_BASE`）。
SETTLE_KEYS = ("taker_settled",)


class _ListFixture(TenantTestCase):
    """本文件的共用夹具：一个真实登录的客服 + 列表取数工具。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.client = make_logged_in_client(
            username="cols-cs", groups=("客服",), nodes=LIST_NODES
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 本文件的读者都要看到 `self.game` 下的单；没有档案 / 没勾游戏 ⇒ 读侧空结果。
        grant_games(User.objects.get(username="cols-cs"), self.game)

    def _order(self, no, **kw):
        return Order.objects.create(
            game=self.game, order_no=no, status=OrderStatus.COMPLETED, **kw
        )

    def _items(self, client=None, query=""):
        """取信封里的 `items`；先断言端点本身成功（前置哨兵）。

        端点失败（403/500）时若不先断业务码，取值会以 `KeyError` 的形式炸在别处、
        把归因引到错误的层。
        """
        body = (client or self.client).get(LIST_URL + query).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]

    def _row(self, no, client=None):
        """取**指定订单号**那一行；订单号不在列表里就响亮失败（非空前置）。"""
        rows = {row["order_no"]: row for row in self._items(client=client)}
        self.assertIn(no, rows, rows)
        return rows[no]


class OrderListColumnValuesTests(_ListFixture):
    """逐键断值：6 个模型列 + 3 个派生/新增列（spec §5 的表逐行）。"""

    NO = "20260923000000000001"

    def setUp(self):
        super().setUp()
        self.publisher = User.objects.create_user(username="cols-publisher")
        self.completed_at = datetime(2026, 9, 23, 10, 30, 0, tzinfo=dt_timezone.utc)
        self._order(
            self.NO,
            game_role_name="钟离",
            customer_ww="旺旺-8899",
            hall_price=Decimal("88.00"),
            sales_agent="销售客服小张",
            created_by="录单人小李",
            remark="客户要求晚上打",
            total_price=Decimal("200.00"),
            received_price=Decimal("150.00"),
            refund_fee=Decimal("20.00"),
            hall_publisher=self.publisher,
            completed_at=self.completed_at,
            time_requirement="2天3小时",
        )

    def test_the_payload_carries_all_ten_contract_keys(self):
        """载荷承诺的 10 个键**一个不少**。

        ⚠️ 本用例**不**断言「没有别的键」：载荷本来就带既有键（`order_no` 等），
        契约只承诺这 10 个是**加法**。`TEN_KEYS` 抄的是 spec §5（+ Task 3 补的第 10 个）的
        原文清单，不是从被测代码导出的常量（否则改名/漏键时会跟着一起变、判据自指）。
        """
        row = self._row(self.NO)
        self.assertEqual(
            set(TEN_KEYS) - set(row), set(), f"缺键：{set(TEN_KEYS) - set(row)}"
        )

    def test_the_seven_model_columns_carry_their_values(self):
        row = self._row(self.NO)
        self.assertEqual(row["game_role_name"], "钟离")
        self.assertEqual(row["customer_ww"], "旺旺-8899")
        self.assertEqual(Decimal(row["hall_price"]), Decimal("88.00"))
        self.assertEqual(row["sales_agent"], "销售客服小张")
        self.assertEqual(row["created_by"], "录单人小李")
        self.assertEqual(row["remark"], "客户要求晚上打")
        # 第 10 个键：模型列，直接回存进去的原值。
        self.assertEqual(row["time_requirement"], "2天3小时")

    def test_net_received_is_the_two_decimal_string(self):
        row = self._row(self.NO)
        # 200.00 − 20.00，字面量（不在测试里重算算式）
        self.assertEqual(row["net_received"], "180.00")

    def test_hall_publisher_name_is_the_display_name(self):
        """发单人那格取**显示名**（昵称优先；2026-10-12 业务方口径，E-0238 同批）。"""
        self.publisher.first_name = "发单人老王"
        self.publisher.save(update_fields=["first_name"])
        row = self._row(self.NO)
        self.assertEqual(row["hall_publisher_name"], "发单人老王")

    def test_hall_publisher_name_falls_back_to_username_when_nickname_empty(self):
        """昵称为空 ⇒ 回落登录账号（`user_display_name` 的既定口径，不发明第二套）。"""
        row = self._row(self.NO)
        self.assertEqual(row["hall_publisher_name"], "cols-publisher")

    def test_completed_at_is_an_iso_string_of_the_stored_instant(self):
        row = self._row(self.NO)
        self.assertEqual(datetime.fromisoformat(row["completed_at"]), self.completed_at)

    def test_the_new_columns_have_empty_shapes_when_never_set(self):
        """历史行（两列天然为空）的载荷形状：`null` / 空串 / 金额字符串。"""
        order = self._order("20260923000000000002")
        row = self._row(order.order_no)
        self.assertIsNone(row["completed_at"])
        self.assertEqual(row["hall_publisher_name"], "")
        self.assertEqual(row["net_received"], "0.00")  # total_price 默认 0.00
        self.assertEqual(row["game_role_name"], "")
        self.assertEqual(row["sales_agent"], "")
        self.assertEqual(row["created_by"], "")
        self.assertEqual(row["remark"], "")
        self.assertEqual(row["time_requirement"], "")


class NetReceivedPayloadTests(_ListFixture):
    """实收款 = `settlement_amount.net_received(order)` 的两位小数字符串。

    新口径（D-0142）：`net_received = total_price − refund_fee`，**不读** `received_price`。
    四档边界全用**字面量**：有实收（陷阱值）/ 实收为 `None` / 负退款 / `0.00`（陷阱值）。
    ⚠️ `received_price` 在这四条里全部取**与实际入参不同的陷阱值**——谁把它读回来就红。
    """

    def _net(self, no, **kw):
        return self._row(self._order(no, **kw).order_no)["net_received"]

    def test_the_payload_ignores_the_received_price(self):
        """旧名 `test_received_price_is_used_when_set`：判据反转 ⇒ 期望 `999.00`。"""
        self.assertEqual(
            self._net(
                "20260923000000000101",
                total_price=Decimal("999.00"),
                received_price=Decimal("150.00"),
                refund_fee=Decimal("0.00"),
            ),
            "999.00",
        )

    def test_received_price_none_falls_back_to_total_price(self):
        """总价是**唯一来源**（不再是「回落档」）：`123.45 − 3.45 = 120.00`。

        期望值与旧口径相同（`None` 时本来就取总价）——保留它钉子的是「缺失的实收款
        不改变答案」这条事实。`received_price=None` 仍是本条夹具的差异点。
        """
        self.assertEqual(
            self._net(
                "20260923000000000102",
                total_price=Decimal("123.45"),
                received_price=None,
                refund_fee=Decimal("3.45"),
            ),
            "120.00",
        )

    def test_a_zero_received_price_no_longer_shrinks_the_net(self):
        """旧名 `test_zero_received_price_is_a_value_not_a_missing_one`：`0.00` 是陷阱值。

        旧口径下期望是 `0.00`；新口径下毛收款只看总价 ⇒ `999.00`。
        """
        self.assertEqual(
            self._net(
                "20260923000000000103",
                total_price=Decimal("999.00"),
                received_price=Decimal("0.00"),
                refund_fee=Decimal("0.00"),
            ),
            "999.00",
        )

    def test_a_negative_refund_adds_to_the_net(self):
        """退款为负 = 冲抵（D-0015：金额可以为负）：`0.00 − (-15.00) = 15.00`。

        旧口径下毛收款取 `received_price = 60.00`（期望 `75.00`）；新口径下毛收款是
        总价 `0.00` ⇒ `15.00`。
        """
        self.assertEqual(
            self._net(
                "20260923000000000104",
                total_price=Decimal("0.00"),
                received_price=Decimal("60.00"),
                refund_fee=Decimal("-15.00"),
            ),
            "15.00",
        )

    def test_a_negative_net_is_carried_as_is(self):
        """负净额照实出参：夹具把毛收款改成**总价 `-25.00`**（`received_price = 7.77` 是陷阱值）。"""
        self.assertEqual(
            self._net(
                "20260923000000000105",
                total_price=Decimal("-25.00"),
                received_price=Decimal("7.77"),
                refund_fee=Decimal("0.00"),
            ),
            "-25.00",
        )

    def test_payload_delegates_to_the_shared_function(self):
        """同源钉：载荷必须经 `settlement_amount.net_received`，桩一换载荷跟着变。

        判别力：把 `get_net_received` 改成自己写一遍算式（哪怕当下结果一模一样），
        本用例变红——载荷会是真算式的值，而不是桩值。
        """
        self._order("20260923000000000106", received_price=Decimal("1.00"))
        with mock.patch(
            "apps.business.api.serializers.settlement_amount.net_received",
            return_value=Decimal("4321.00"),
        ) as spy:
            items = self._items()
        self.assertEqual(len(items), 1)  # 非空哨兵：真的序列化了一行
        self.assertEqual(items[0]["net_received"], "4321.00")
        self.assertEqual(spy.call_count, 1)  # 逐行调用，不是别处顺手调的一次


class ListOfferPriceTests(_ListFixture):
    """发单价列：`/orders/` 带 `hall_price`，并按 `order.restrict_view_offer_price` 脱敏。

    spec §5 / C6：载荷**原本刻意不含**它（只有大厅载荷含），本轮补进列表；
    脱敏声明在 `OrderListSerializer` 自己的 `REDACTED_PRICE_FIELDS` 上。
    """

    NO = "20260923000000000201"

    def setUp(self):
        super().setUp()
        self._order(self.NO, hall_price=Decimal("88.00"))

    def test_a_reader_without_the_flag_sees_the_model_offer_price(self):
        # 阴性对照：不能把「所有人都 None」当成脱敏生效
        self.assertEqual(Decimal(self._row(self.NO)["hall_price"]), Decimal("88.00"))

    def test_a_restricted_reader_gets_none_with_the_key_still_present(self):
        restricted = make_logged_in_client(
            username="cols-restricted",
            groups=("客服",),
            nodes=(*LIST_NODES, OFFER_FLAG),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 受限的是发单价**值**（flag 脱敏），不是这一行的可见性：本用例要的是「行在、值被脱敏」，
        # 所以这位读者必须看得到本单所属的游戏（补游戏权限，不动 flag / 节点 / 角色）。
        grant_games(User.objects.get(username="cols-restricted"), self.game)
        row = self._row(self.NO, client=restricted)
        self.assertIn("hall_price", row)       # 键恒在（前端契约）
        self.assertIsNone(row["hall_price"])   # 值被脱敏

    def test_the_list_shows_the_model_price_not_the_effective_taker_price(self):
        """列表取的是 `Order.hall_price`（录单默认发单价）——**不是**大厅那份生效接单价。

        判别力：把基类 `hall_price` 也写成 `source=EFFECTIVE_TAKER_PRICE`（= 让列表读覆盖率
        的那支）⇒ 本用例读到 55.00（列表侧没有那个注解时直接取不到值）而变红。
        spec §5 的表里这一格是「模型字段」。
        """
        order = self._order("20260923000000000202", hall_price=Decimal("88.00"))
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, taker_price=Decimal("55.00")
        )
        self.assertEqual(
            Decimal(self._row(order.order_no)["hall_price"]), Decimal("88.00")
        )


class HallKeepsWorkingAndGainsTheKeysTests(TenantTestCase):
    """大厅载荷继承同一份 `fields` ⇒ 跟着多这些键，且**不 500**（spec §5 的纯加法）。

    先例同族：`source` / `taker_name` 也是这么加进大厅的（登记为纯加法）。
    """

    NO = "20260923000000000301"

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.publisher = User.objects.create_user(username="hall-publisher")
        self.completed_at = datetime(2026, 9, 23, 11, 0, 0, tzinfo=dt_timezone.utc)
        self.pool = Order.objects.create(
            game=self.game,
            order_no=self.NO,
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("66.00"),
            game_role_name="胡桃",
            customer_ww="ww-1",
            sales_agent="客服A",
            created_by="录单B",
            remark="备注C",
            received_price=Decimal("9.99"),      # 陷阱值：新口径下不参与净实收
            total_price=Decimal("70.00"),        # ⭐ 毛收款 = 订单总价（D-0142）
            refund_fee=Decimal("5.00"),
            hall_publisher=self.publisher,
            completed_at=self.completed_at,
            time_requirement="2天3小时",
        )
        # 大厅只列「有 MAIN 绩效行、且还没人接」的单（`search_hall` 的两个条件）。
        Performance.objects.create(
            order=self.pool, emp_type=EmpType.MAIN, temp_amount=Decimal("66.00")
        )
        self.taker = make_logged_in_client(
            username="hall-taker", groups=("代练员",), nodes=HALL_NODES
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 大厅与订单列表同一维度收窄：代练员要看到 `self.game` 下那张池内单才谈得上载荷。
        grant_games(User.objects.get(username="hall-taker"), self.game)

    def test_hall_payload_carries_the_columns_and_does_not_500(self):
        body = self.taker.get(HALL_URL).json()
        self.assertEqual(body["code"], 0, body)
        rows = {row["order_no"]: row for row in body["data"]["items"]}
        self.assertIn(self.NO, rows)  # 非空哨兵：大厅里真的有这一行
        row = rows[self.NO]
        self.assertEqual(
            set(TEN_KEYS) - set(row), set(), f"缺键：{set(TEN_KEYS) - set(row)}"
        )
        # 大厅的 `hall_price` 仍是**生效接单价**（基类字段被本类的声明字段覆盖）
        self.assertEqual(Decimal(row["hall_price"]), Decimal("66.00"))
        self.assertEqual(row["game_role_name"], "胡桃")
        self.assertEqual(row["hall_publisher_name"], "hall-publisher")
        self.assertEqual(row["net_received"], "65.00")
        self.assertEqual(datetime.fromisoformat(row["completed_at"]), self.completed_at)
        # 第 10 个键：大厅继承基类 `fields` ⇒ 也带它（同一张单的同一列）。
        self.assertEqual(row["time_requirement"], "2天3小时")


class HallPublisherNameQueryTests(_ListFixture):
    """`hall_publisher_name` 不许 N+1：SQL 条数**不随行数增长**。

    判别力：把 `selectors.search_orders` 里的 `select_related("hall_publisher")` 去掉 ⇒
    5 行那次会多出 4 条取用户的 SQL ⇒ 本用例变红。
    """

    def _count_queries(self):
        with CaptureQueriesContext(connection) as captured:
            self._items()
        return len(captured)

    def _make_row(self, index):
        publisher = User.objects.create_user(username=f"n1-publisher-{index}")
        self._order(
            f"202609230000000004{index:02d}",
            hall_publisher=publisher,
        )

    def test_query_count_is_flat_between_one_and_five_rows(self):
        self._make_row(1)
        one_row = self._count_queries()
        for index in range(2, 6):
            self._make_row(index)
        assert len(self._items()) == 5  # 非空哨兵：第二次真的序列化了 5 行
        five_rows = self._count_queries()
        self.assertEqual(one_row, five_rows)


class UnattributedPublisherQueryTests(_ListFixture):
    """财务「待归属」清单也不许给 `hall_publisher_name` 出 N+1（F4，2026-09-23 收口）。

    该端点（`GET /api/business/settlements/unattributed`）用**同一个** `OrderListSerializer`
    出载荷（`UnattributedView.get_queryset()`），所以「解引用 FK 取发单人名字」这件事在它上面
    同样暴露。判别力：去掉 `UnattributedView.get_queryset()` 的
    `select_related("hall_publisher")` ⇒ 5 行那次会多出 4 条取用户的 SQL ⇒ 本用例红。

    ⚠️ 与本文件另一条查询数用例同形（1 行 vs 5 行**总数相等**），但**必须独立**：两条走的是
    两个不同的 queryset（`selectors.search_orders` 与本视图），只改一处不会被对方发现。
    """

    def setUp(self):
        super().setUp()
        # 财务读端点的门是节点 `finance.payroll`（`FinanceReadPermissionMixin`），
        # 与订单列表的 `order.list` 不是同一个；这里用 superuser 跳过建角色目录
        # （「谁被拦」由 `tests/test_settlement_api.py` 的权限用例双向钉住）。
        self.finance = make_logged_in_client(
            username="cols-unattr-finance", is_superuser=True
        )

    def _rows(self):
        """取「待归属」清单的 `items`；先断端点本身成功（前置哨兵）。"""
        body = self.finance.get(UNATTRIBUTED_URL).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]

    def _count_queries(self):
        with CaptureQueriesContext(connection) as captured:
            self._rows()
        return len(captured)

    def _make_row(self, index):
        """一条 `COMPLETED` 且**没有销售提成行**的订单（= 待归属）+ 一个**不同的**发单人。"""
        publisher = User.objects.create_user(username=f"unattr-publisher-{index}")
        return self._order(
            f"202609230000000005{index:02d}",
            hall_publisher=publisher,
        )

    def test_query_count_is_flat_between_one_and_five_rows(self):
        self._make_row(1)
        one_row = self._count_queries()
        for index in range(2, 6):
            self._make_row(index)
        rows = self._rows()
        # 非空哨兵 1：真的序列化了 5 行（空清单上「不涨」是恒真的假绿）。
        self.assertEqual(len(rows), 5)
        # 非空哨兵 2：那几行的发单人**真的非空**——N+1 只在解引用 FK 时发生，
        # 全空的话 `hall_publisher_name` 走的是 `""` 分支，本用例会空转。
        self.assertTrue(all(row["hall_publisher_name"] for row in rows), rows)
        five_rows = self._count_queries()
        self.assertEqual(one_row, five_rows)


# ── 跨租户：两个新列的隔离（spec §8 #8 / `AGENTS.md` §4 的红线）────────────
#: 租户 A = `TenantTestCase` 的 `test` schema（域名与既有隔离测试同口径）。
DOMAIN_A = "tenant.test.com"
#: 租户 B 的域名：与 A 不同；`.test.local` 在 `.env` 的 `DJANGO_ALLOWED_HOSTS` 白名单里。
DOMAIN_B = "olp2-cols-b.test.local"
SCHEMA_B = "olp2_cols_b"

#: 判据用的业务值：**租户内唯一、且两租户取值不同**——不是主键。
NO_A = "20260923000000000901"
NO_B = "20260923000000000902"
PUB_A = "iso-cols-pub-a"
PUB_B = "iso-cols-pub-b"
DONE_A = datetime(2031, 1, 2, 3, 4, 5, tzinfo=dt_timezone.utc)
DONE_B = datetime(2032, 6, 7, 8, 9, 10, tzinfo=dt_timezone.utc)


class NewColumnsCrossTenantTests(TenantTestCase):
    """`hall_publisher_name` / `completed_at` 的跨租户隔离。

    ⚠️ 判据落在**订单号**（行可见性）与**发单人用户名 / 完单时间**（非主键值）上：
    两个 schema 的主键序列各自从 1 开始 ⇒ `filter(id=a_row.id)` 在 B 里命中的是 B 自己的
    行（`AGENTS.md` §5 / E-0023 的同型坑）。
    ⚠️ 先建 B 的行、**后**取 A 的列表：在 B 建行之前取的快照里本来就没有 B 的值，
    那两条否定断言会恒真（恒真的断言看起来比没测更可靠）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="列载荷隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # 跨域名请求不会在响应结束时还原 schema（`TenantMainMiddleware` 只进不出），
        # 上一条用例可能把连接留在 B 上 ⇒ 这里显式钉回 A。
        connection.set_tenant(self.tenant)
        self.client = make_logged_in_client(
            domain=DOMAIN_A, username="iso-cols-cs-a", groups=("客服",), nodes=LIST_NODES
        )

    def _make(self, order_no, publisher_username, completed_at):
        """在当前 schema 里造「游戏 + 发单人 + 已完成订单」。"""
        game = Game.objects.create(name=f"游戏-{order_no}")
        publisher = User.objects.create_user(username=publisher_username)
        return Order.objects.create(
            game=game,
            order_no=order_no,
            status=OrderStatus.COMPLETED,
            hall_publisher=publisher,
            completed_at=completed_at,
        )

    def _rows(self, client, domain):
        """取 `{order_no: row}`；先断言端点本身成功（前置哨兵）。"""
        body = client.get(LIST_URL, SERVER_NAME=domain).json()
        self.assertEqual(body["code"], 0, body)
        return {row["order_no"]: row for row in body["data"]["items"]}

    @staticmethod
    def _publishers(rows):
        return [row["hall_publisher_name"] for row in rows.values()]

    @staticmethod
    def _completed_instants(rows):
        """把 `completed_at` 解析回时间点（空值跳过）——判据用**值**，不用字符串形状。"""
        return [
            datetime.fromisoformat(row["completed_at"])
            for row in rows.values()
            if row["completed_at"]
        ]

    def test_new_columns_do_not_leak_across_tenants(self):
        order_a = self._make(NO_A, PUB_A, DONE_A)
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # A 侧读者要能看到**自己租户、自己游戏**那一单（否则正向对照与「行在」都是空谈）；
        # 补的是游戏权限，跨租户的否定断言一位没动。
        grant_games(User.objects.get(username="iso-cols-cs-a"), order_a.game)

        try:
            with tenant_context(self.tenant_b):
                order_b = self._make(NO_B, PUB_B, DONE_B)
                b_client = make_logged_in_client(
                    domain=DOMAIN_B,
                    username="iso-cols-cs-b",
                    groups=("客服",),
                    nodes=LIST_NODES,
                )
                # 同上，B 侧读者只授 B 自己那款游戏。
                grant_games(User.objects.get(username="iso-cols-cs-b"), order_b.game)
                b_rows = self._rows(b_client, DOMAIN_B)
        finally:
            # B 侧真实登录 + B 域名请求都会把连接留在 B 上（见 setUp 的说明）。
            connection.set_tenant(self.tenant)

        a_rows = self._rows(self.client, DOMAIN_A)

        # 正向对照 + 非空前置：两侧都看得到**自己**那单，且两个新列真有值
        self.assertEqual(a_rows[NO_A]["hall_publisher_name"], PUB_A)
        self.assertIn(DONE_A, self._completed_instants({NO_A: a_rows[NO_A]}))
        self.assertEqual(b_rows[NO_B]["hall_publisher_name"], PUB_B)
        self.assertIn(DONE_B, self._completed_instants({NO_B: b_rows[NO_B]}))

        # 两个方向都断：订单号（行）+ 发单人用户名 + 完单时间（值）
        self.assertNotIn(NO_B, a_rows)
        self.assertNotIn(PUB_B, self._publishers(a_rows))
        self.assertNotIn(DONE_B, self._completed_instants(a_rows))
        self.assertNotIn(NO_A, b_rows)
        self.assertNotIn(PUB_A, self._publishers(b_rows))
        self.assertNotIn(DONE_A, self._completed_instants(b_rows))


class PaymentStatusPayloadKeyTests(_ListFixture):
    """本批两个新键在三个载荷出口都在（spec §8 第 16 条）：订单列表 / 抢单大厅 / 财务待归属。

    判据形状与 `TEN_KEYS` 那条一致：**缺键集合为空**，不是「恰好这两个」。
    """

    NO = "20260923000000000201"

    def setUp(self):
        super().setUp()
        self._order(
            self.NO,
            payment_status="SETTLING",
            payment_method="花呗",
            total_price=Decimal("88.00"),
            hall_price=Decimal("66.00"),
        )
        # 大厅只列「状态待抢单 + 有 MAIN 绩效行 + 还没人接」的单（`search_hall` 的两个条件）。
        # ⚠️ `_ListFixture._order(no, **kw)` 已经把 `status` 写死成 `COMPLETED`
        # （`Order.objects.create(..., status=OrderStatus.COMPLETED, **kw)`）⇒ 这里**不能**
        # 再传 `status=`（会 `TypeError: got multiple values for keyword argument 'status'`），
        # 建成后用 `.update()` 改状态。
        self.pool = self._order(
            "20260923000000000202",
            payment_status="SETTLED",
            payment_method="信用卡",
            hall_price=Decimal("66.00"),
        )
        Order.objects.filter(order_no=self.pool.order_no).update(
            status=OrderStatus.WAITING_GRAB
        )
        Performance.objects.create(
            order=Order.objects.get(order_no=self.pool.order_no),
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("66.00"),
        )
        self.taker = make_logged_in_client(
            username="paystat-payload-taker", groups=("代练员",), nodes=HALL_NODES
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改。大厅读路径过 `game_scope`：代练员要看到
        # `self.pool` 那一行（非空哨兵 + 两个新键的值）就必须持有这款游戏。
        grant_games(User.objects.get(username="paystat-payload-taker"), self.game)
        self.finance = make_logged_in_client(
            username="paystat-payload-finance", is_superuser=True
        )

    def test_order_list_payload_carries_both_keys(self):
        row = self._row(self.NO)
        self.assertEqual(
            set(PAYSTATUS_KEYS) - set(row),
            set(),
            f"缺键：{set(PAYSTATUS_KEYS) - set(row)}",
        )
        self.assertEqual(row["payment_status"], "SETTLING")
        self.assertEqual(row["payment_method"], "花呗")

    def test_hall_payload_carries_both_keys_without_500(self):
        body = self.taker.get(HALL_URL).json()
        self.assertEqual(body["code"], 0, body)
        rows = {row["order_no"]: row for row in body["data"]["items"]}
        self.assertIn(self.pool.order_no, rows)  # 非空哨兵：大厅里真的有这一行
        row = rows[self.pool.order_no]
        self.assertEqual(set(PAYSTATUS_KEYS) - set(row), set(), f"缺键：{set(PAYSTATUS_KEYS) - set(row)}")
        self.assertEqual(row["payment_status"], "SETTLED")
        self.assertEqual(row["payment_method"], "信用卡")

    def test_unattributed_payload_carries_both_keys_without_500(self):
        body = self.finance.get(UNATTRIBUTED_URL).json()
        self.assertEqual(body["code"], 0, body)
        rows = {row["order_no"]: row for row in body["data"]["items"]}
        self.assertIn(self.NO, rows)  # 非空哨兵
        row = rows[self.NO]
        self.assertEqual(set(PAYSTATUS_KEYS) - set(row), set(), f"缺键：{set(PAYSTATUS_KEYS) - set(row)}")
        self.assertEqual(row["payment_status"], "SETTLING")
        self.assertEqual(row["payment_method"], "花呗")

    def test_neither_key_is_redacted_when_the_offer_price_flag_is_set(self):
        """`payment_method` / `payment_status` 不在价格脱敏面里（脱敏只作用于 `hall_price`）。

        判据与写法照同文件既有的 `OFFER_FLAG` 用例：受限账号读到的仍是真值。
        """
        limited = make_logged_in_client(
            username="paystat-payload-limited",
            groups=("客服",),
            nodes=LIST_NODES + (OFFER_FLAG,),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改。本条要测的是「**价格脱敏**只作用于 `hall_price`，
        # 不作用于本批这两个键」：前提是这位受限读者**看得到这一行**（读侧过
        # `game_scope`）。补游戏权限不动 flag / 节点 / 角色，脱敏面一位没改。
        grant_games(User.objects.get(username="paystat-payload-limited"), self.game)
        row = self._row(self.NO, client=limited)
        self.assertEqual(row["payment_status"], "SETTLING")
        self.assertEqual(row["payment_method"], "花呗")

class CreatedByNameKeyTests(_ListFixture):
    """「录单人」显示名（2026-10-27 业务方：列表要显示**账号名称**，不是登录账号）。

    判据打在**端点真实载荷**上（⛔ 不拿模型对象自己拼），并且必须能分辨：
    ① 账号有昵称 ⇒ 显示昵称；② 账号不存在（改名/硬删后的快照） ⇒ 回落快照本身。
    """

    NO = "20261027000000000001"

    def test_the_payload_carries_the_display_name_of_the_recorder(self):
        User.objects.create_user(username="rec-1", first_name="小九")
        self._order(self.NO, created_by="rec-1")
        row = self._row(self.NO)
        self.assertEqual(row["created_by_name"], "小九")
        # 快照列本身仍然是账号（别把显示名写回快照列）
        self.assertEqual(row["created_by"], "rec-1")

    def test_an_unknown_account_falls_back_to_the_snapshot(self):
        self._order(self.NO, created_by="ghost-account")
        row = self._row(self.NO)
        self.assertEqual(row["created_by_name"], "ghost-account")

    def test_a_missing_recorder_renders_empty(self):
        self._order(self.NO, created_by="")
        row = self._row(self.NO)
        self.assertEqual(row["created_by_name"], "")

    def test_the_display_name_lookups_do_not_grow_with_the_row_count(self):
        """N+1 判据：**多 4 行不多一次账号查询**。

        ⚠️ 断言的是「1 行时与 5 行时的账号显示名查询次数**相等**」，不是「恰好 1 次」：
        这个页面上还有别的读面会碰 `auth_user`（例如登录态本身），钉死 1 次会把
        无关的查询数变化误判成 N+1；判据要的是**不随行数增长**。
        """
        def count_name_lookups() -> int:
            with CaptureQueriesContext(connection) as ctx:
                self._items()
            return len([
                q["sql"] for q in ctx.captured_queries
                if "auth_user" in q["sql"] and "first_name" in q["sql"]
            ])

        User.objects.create_user(username="rec-solo", first_name="唯一")
        self._order("20261027000000000001", created_by="rec-solo")
        one_row = count_name_lookups()

        for i in range(4):
            name = f"rec-more-{i}"
            User.objects.create_user(username=name, first_name=f"多{i}")
            self._order(f"2026102700000000020{i}", created_by=name)
        five_rows = count_name_lookups()

        self.assertEqual(
            five_rows, one_row,
            f"1 行时 {one_row} 次、5 行时 {five_rows} 次 ⇒ 逐行查库了（N+1）",
        )


class SplitKeysAreOrderListOnlyTests(_ListFixture):
    """⭐ 批次 4 线 B（拆单）：两个新键**只在订单列表**，且形态就是契约里写的那样。

    判据三向（缺键 / 值 / **不给别的读面**）：

    - 订单列表载荷**末尾**有两个键（键顺序也钉住——既有前端与「键集合」网的纪律）；
    - 值 = 该组的 `split_id` 与**组内成员总数**（非拆单 = 空串 / `0`）；
    - ⛔ 大厅与回收站载荷**没有**这两个键。那是本线对 spec §2.4 的一处**执行期改准**
      （`OrderSplitListSerializer` 的 docstring 记着按事实逐条写清的理由：那两处有自己的键表
      判据，且都不消费这两个键）。钉住它是为了让「哪天有人把它们挪进基类」当场红
      ——那一刻必须**同一次**改 `jidanpu_common.REMOTE_ROW_BASE`（**上游镜像行**的键表）与
      `tests/test_order_recycle.RECYCLE_ROW_KEYS`。

    ⚠️ **证明力边界（`AGENTS.md` §5-C 那条）**：本类对**订单列表自己**那一条是**包含式**
    （`set(SPLIT_KEYS) - set(row) == ∅`，外加「两键在末尾」的**顺序**判据），**不是**整个
    载荷的「恰好集合」——载荷本来就带既有键（`order_no` 等几十个），钉成恰好集合会连带把
    既有键全钉死、超出本线范围。**「恰好集合」只覆盖两处**：① 回收站载荷
    （`tests/test_order_recycle.py` 的 `RECYCLE_ROW_KEYS`）；② 本类第三向那两条
    `assertNotIn`（钉「这两个键**不在**另外两份载荷里」）。⛔ 别把本类读成
    「订单列表载荷的键集合被恰好钉住了」。
    """

    NO = "20260923000000000601"
    POOL_NO = "20260923000000000602"
    BIN_NO = "20260923000000000603"
    GROUP = "70000000000000000001"

    def setUp(self):
        super().setUp()
        self._order(self.NO, hall_price=Decimal("66.00"), split_id=self.GROUP)
        self._order("20260923000000000604", split_id=self.GROUP, project_text="子项目")
        # 大厅那一档：状态待抢单 + 一条无主的 MAIN 绩效行
        self.pool = self._order(self.POOL_NO, hall_price=Decimal("66.00"))
        Order.objects.filter(order_no=self.POOL_NO).update(
            status=OrderStatus.WAITING_GRAB
        )
        Performance.objects.create(
            order=Order.objects.get(order_no=self.POOL_NO),
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("66.00"),
        )
        self.taker = make_logged_in_client(
            username="split-keys-taker", groups=("代练员",), nodes=HALL_NODES
        )
        # （按游戏权限收窄）：夹具补齐，不是放宽判据。
        grant_games(User.objects.get(username="split-keys-taker"), self.game)
        self.recycler = make_logged_in_client(
            username="split-keys-recycler",
            groups=("客服",),
            nodes=LIST_NODES + ("order.recycle",),
        )
        grant_games(User.objects.get(username="split-keys-recycler"), self.game)

    def test_the_two_keys_are_at_the_end_of_the_order_list_row(self):
        row = self._row(self.NO)
        self.assertEqual(
            set(SPLIT_KEYS) - set(row), set(), f"缺键：{set(SPLIT_KEYS) - set(row)}"
        )
        self.assertEqual(row["split_id"], self.GROUP)
        self.assertEqual(row["split_members"], 2)
        # ⭐ D-0357（G16）：拆单键之后**再**追加两键（组内已完成数 / 本行是否主单），
        # 仍摆在**末尾**——末尾四键的顺序一并钉住。
        self.assertEqual(
            set(SPLIT_V2_KEYS) - set(row), set(),
            f"缺键：{set(SPLIT_V2_KEYS) - set(row)}",
        )
        self.assertEqual(list(row)[-6:], list(SPLIT_KEYS) + list(SPLIT_V2_KEYS))
        # 本夹具的 `_order` 落库即 COMPLETED ⇒ 组内两笔都完成 ⇒ X=2；
        # 主单（组内 id 最小者）行 split_is_main=True
        self.assertEqual(row["split_completed_members"], 2)
        self.assertIs(row["split_is_main"], True)
        self.assertIs(row["split_is_balance_anchor"], True)

    def test_a_plain_row_carries_the_empty_shapes(self):
        row = self._row(self.pool.order_no)
        self.assertEqual(row["split_id"], "")
        self.assertEqual(row["split_members"], 0)
        # ⭐ D-0357（G16）：非拆单行的两键恒 `0` / `False`（字面口径）
        self.assertEqual(row["split_completed_members"], 0)
        self.assertIs(row["split_is_main"], False)
        self.assertIs(row["split_is_balance_anchor"], False)

    def test_the_hall_payload_does_not_carry_them(self):
        body = self.taker.get(HALL_URL).json()
        self.assertEqual(body["code"], 0, body)
        rows = {row["order_no"]: row for row in body["data"]["items"]}
        self.assertIn(self.POOL_NO, rows)  # 非空哨兵：大厅里真的有这一行
        for key in SPLIT_KEYS:
            self.assertNotIn(key, rows[self.POOL_NO], f"大厅载荷不该有 {key}")

    def test_the_recycle_bin_payload_does_not_carry_them(self):
        self._order(self.BIN_NO)
        Order.objects.filter(order_no=self.BIN_NO).update(
            deleted_at=datetime(2026, 9, 23, 12, 0, 0, tzinfo=dt_timezone.utc),
            deleted_by="cols-cs",
        )
        body = self.recycler.get("/api/business/orders/recycle-bin/").json()
        self.assertEqual(body["code"], 0, body)
        rows = {row["order_no"]: row for row in body["data"]["items"]}
        self.assertIn(self.BIN_NO, rows)  # 非空哨兵
        for key in SPLIT_KEYS:
            self.assertNotIn(key, rows[self.BIN_NO], f"回收站载荷不该有 {key}")


class TakerSettledKeyTests(_ListFixture):
    """⭐ **2026-10-21（钱包发薪线）**：`taker_settled` 的口径。

    「该单**打手绩效**（`Performance.emp_type=MAIN`）是否已结账」——管理订单页「发单价」列
    在金额前那个绿色「结」标记的取数口。判据 = 模型属性 `Performance.is_settled`
    （`settle_status == 已结账` **或** `settled_at is not None` **或** `settled_amount is not None`）。

    分母（**没有 MAIN 行**）⇒ `False`（不是 `null`）：语义是「这张单没有可结账的打手绩效」。
    本类逐条钉住：已结账 ⇒ `True`；未结账 ⇒ `False`；没有 MAIN 行 ⇒ `False`；大厅行 ⇒ 键在、
    值 `False`；三字段并集里**只看 `settle_status` 会判错**的两条脏行 ⇒ `True`。
    外加一条**查询数不随行数增长**的判据（⛔ 不许退回 `SerializerMethodField` 里逐行查）。
    """

    SETTLED_NO = "20261021000000000071"
    UNSETTLED_NO = "20261021000000000072"
    NO_MAIN_NO = "20261021000000000073"
    HALL_NO = "20261021000000000074"

    def setUp(self):
        super().setUp()
        self._order(self.SETTLED_NO, hall_price=Decimal("66.00"))
        Performance.objects.create(
            order=Order.objects.get(order_no=self.SETTLED_NO),
            user=User.objects.create_user(username="settled-taker"),
            emp_type=EmpType.MAIN,
            settle_status=SettleStatus.SETTLED,
        )
        self._order(self.UNSETTLED_NO)
        Performance.objects.create(
            order=Order.objects.get(order_no=self.UNSETTLED_NO),
            user=User.objects.create_user(username="unsettled-taker"),
            emp_type=EmpType.MAIN,
            settle_status=SettleStatus.UNSETTLED,
        )
        # 分母：一条 MAIN 行都没有的普通单（= 未接单 / 历史遗留）。
        self._order(self.NO_MAIN_NO)
        # 大厅那一档：待抢单 + 一条**无主** MAIN 行。
        self.pool = self._order(self.HALL_NO, hall_price=Decimal("66.00"))
        Order.objects.filter(order_no=self.HALL_NO).update(
            status=OrderStatus.WAITING_GRAB
        )
        Performance.objects.create(
            order=Order.objects.get(order_no=self.HALL_NO),
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("66.00"),
        )
        self.taker = make_logged_in_client(
            username="settle-key-taker", groups=("代练员",), nodes=HALL_NODES
        )
        grant_games(User.objects.get(username="settle-key-taker"), self.game)

    def test_settled_main_performance_makes_the_key_true(self):
        row = self._row(self.SETTLED_NO)
        self.assertEqual(
            set(SETTLE_KEYS) - set(row), set(), f"缺键：{set(SETTLE_KEYS) - set(row)}"
        )
        self.assertIs(row["taker_settled"], True)

    def test_unsettled_main_performance_makes_the_key_false(self):
        row = self._row(self.UNSETTLED_NO)
        self.assertIs(row["taker_settled"], False)

    def test_an_order_without_main_performance_is_false(self):
        """没有 MAIN 行 ⇒ `False`（⛔ 不是 `null`）：大厅行 / 未接单走同一档。"""
        row = self._row(self.NO_MAIN_NO)
        self.assertIs(row["taker_settled"], False)

    def test_the_three_field_union_is_the_judgement(self):
        """`settle_status` 仍写「未结账」、但留着 `settled_at` / `settled_amount` ⇒ **算已结账**。

        判别力：把表达式改成只看 `settle_status` ⇒ 这两条落 `False`、本用例红。
        口径必须与 `Performance.is_settled` 同源（那三条字段并集是为存量脏行加的，见其 docstring）。
        """
        dirty = (
            {"settled_at": datetime(2026, 10, 20, 12, 0, 0, tzinfo=dt_timezone.utc)},
            {"settled_amount": Decimal("1.00")},
        )
        expected = {}
        for index, kwargs in enumerate(dirty):
            no = f"2026102100000000008{index}"
            order = self._order(no)
            # 状态列**故意**留默认的「未结账」：只有另外两列留下痕迹。
            self.assertEqual(
                Performance.objects.create(
                    order=order, emp_type=EmpType.MAIN, **kwargs
                ).settle_status,
                SettleStatus.UNSETTLED,
            )
            expected[no] = True
        for no, want in expected.items():
            self.assertIs(self._row(no)["taker_settled"], want, no)

    def test_hall_row_carries_the_key_and_it_is_false(self):
        body = self.taker.get(HALL_URL).json()
        self.assertEqual(body["code"], 0, body)
        rows = {row["order_no"]: row for row in body["data"]["items"]}
        self.assertIn(self.HALL_NO, rows, rows)  # 非空哨兵：大厅里真的有这一行
        self.assertIn("taker_settled", rows[self.HALL_NO])
        self.assertIs(rows[self.HALL_NO]["taker_settled"], False)

    def test_query_count_does_not_grow_with_row_count(self):
        """⛔ 不许 N+1：行数从 4 涨到 9，SQL 条数**不变**。

        判别力：把取值改成 `SerializerMethodField` 里逐行
        `Performance.objects.filter(...)` ⇒ 5 行那次会多出 5 条 SQL ⇒ 本用例红。
        """
        def count():
            with CaptureQueriesContext(connection) as captured:
                return len(captured), self._items()

        small_count, rows = count()
        self.assertEqual(len(rows), 4, rows)  # 非空前置：本类 setUp 建了 4 条
        for index in range(1, 6):
            order = self._order(f"2026102100000000009{index}")
            Performance.objects.create(
                order=order,
                emp_type=EmpType.MAIN,
                settle_status=SettleStatus.SETTLED,
            )
        big_count, rows = count()
        self.assertEqual(len(rows), 9, rows)  # 非空哨兵：第二次真的序列化了 9 行
        self.assertEqual(small_count, big_count)


#: ⭐ **来源字典当前名**：订单列表载荷新增 `source_name` ——
#: 管理订单页「订单来源」列的显示值（挂字典 ⇒ 字典**当前名**；没挂字典 ⇒ `source` 快照）。
#: ⚠️ 与 `SPLIT_KEYS` 同一处置：**只**加在 `OrderSplitListSerializer`（订单列表这一条读面），
#: ⛔ 大厅 / 回收站 / 待归属**不加**——那三处各有自己那套键表（`RECYCLE_ROW_KEYS` /
#: `HallListSerializer.Meta.fields` ↔ `REMOTE_ROW_BASE`），且都不显示这一列。
#: 逐条判据（改名 / 删字典 / 自由文本 / 查询数）在
#: `tests/test_order_source_intake.py::OrderListSourceDisplayNameTests`。
SOURCE_NAME_KEYS = ("source_name",)


class SourceNameKeyTests(_ListFixture):
    """⭐ 2026-10-23：订单列表载荷带 `source_name`（那一列的显示值）。

    本类只钉**键在 + 无字典时等于快照**（本夹具**没有**「挂字典」的现成造法；字典那几档在
    `tests/test_order_source_intake.py::OrderListSourceDisplayNameTests`，那里有字典夹具）。
    """

    def test_the_key_is_present_and_falls_back_to_the_snapshot(self):
        """没挂字典 ⇒ `source_name` == `source`；键缺了就红（加键必须同一次登记契约）。"""
        self._order("20261023000000000201", source="自由文本来源")
        row = self._row("20261023000000000201")
        self.assertEqual(
            set(SOURCE_NAME_KEYS) - set(row), set(),
            f"缺键：{set(SOURCE_NAME_KEYS) - set(row)}",
        )
        self.assertEqual(row["source_name"], "自由文本来源")
        self.assertEqual(row["source"], "自由文本来源")


#: ⭐ **业务方**：订单列表载荷新增 `order_type`（陪玩单徽标）与 `tag`
#: （标记徽标）。两者都是 `Order` 的真列（无派生、无额外查询），**只加在订单列表**
#: （`OrderSplitListSerializer`）这一条读面上；大厅 / 回收站 / 待归属各有自己的「恰好集合」
#: 键表，本线不动它们。
ORDER_TYPE_TAG_KEYS = ("order_type", "tag")


#: ⭐ 订单列表载荷的**恰好**键集合（逐字列出，⛔ 不从
#: `OrderSplitListSerializer.Meta.fields` 派生——自指断言在加键 / 改名时会跟着一起变、
#: 永远绿）。它此前只有「缺键集合为空」那半边（`TEN_KEYS` / `PAYSTATUS_KEYS` / …）。
#: ⚠️ 只覆盖**订单列表**；大厅 / 回收站 / 待归属的恰好集合网在
#: `RECYCLE_ROW_KEYS` / `REMOTE_ROW_BASE` 那两处，本线一个字没动。
ORDER_LIST_ROW_KEYS = frozenset({
    "created_by_name",
    "order_no",
    "source_order_no",
    "source",
    "status",
    "game",
    "server",
    "project_text",
    "total_price",
    "customer_phone",
    "is_urgent",
    "created_at",
    "refund_fee",
    "taker_name",
    "game_role_name",
    "customer_ww",
    "hall_price",
    "sales_agent",
    "created_by",
    "remark",
    "completed_at",
    "time_requirement",
    "payment_status",
    "payment_method",
    "net_received",
    "hall_publisher_name",
    "taker_settled",
    "platform_push",
    "source_name",
    "order_type",
    "tag",
    "taker_is_platform",
    # ⭐ 新老客标签（录单快照 + 是否手动覆盖）——只加在订单列表这一条读面。
    "customer_kind",
    "customer_kind_is_manual",
    "split_kind",
    "split_id",
    "split_members",
    # ⭐ D-0357（G16）：拆单组读数两键（组内已完成数 / 本行是否主单）。
    "split_completed_members",
    "split_is_main",
    "split_is_balance_anchor",
})


class OrderListExactKeysTests(_ListFixture):
    """⭐ 2026-10-28：订单列表载荷的键集合**恰好** = `ORDER_LIST_ROW_KEYS`（多一个、少一个都红）。

    为什么补这一条：本仓以往的判据只有「缺键集合为空」（`TEN_KEYS` 那套），它挡不住**另一端**
    ——前端读了一个后端**不回**的键 ⇒ `escapeHtml(undefined)` 在 layui 的 `parseData` 里抛、
    整张表一行都渲染不出来（T6 抓到的致命缺陷）。订单列表是本仓列最多、改动最频繁的一页，
    「恰好」是唯一能在这页挡住那种缺陷的形状。

    ⚠️ 判据只覆盖订单列表：大厅 / 回收站 / 待归属各有自己的恰好集合网，本线不动。
    """

    NO = "20261028000000000601"

    def test_the_list_row_key_set_is_exactly_the_promise(self):
        self._order(self.NO)
        rows = self._items()
        self.assertEqual(len(rows), 1, rows)  # 非空哨兵：空表上「键集相等」是恒假的绿
        self.assertEqual(set(rows[0]), ORDER_LIST_ROW_KEYS)

    def test_order_type_and_tag_carry_their_real_values(self):
        """两键是**真列**（不是造出来的假值）：陪玩单 / 标记逐字回出。"""
        self._order(
            self.NO, order_type=OrderType.COMPANION, tag="老板指定打手"
        )
        row = self._row(self.NO)
        self.assertEqual(
            set(ORDER_TYPE_TAG_KEYS) - set(row), set(),
            f"缺键：{set(ORDER_TYPE_TAG_KEYS) - set(row)}",
        )
        self.assertEqual(row["order_type"], OrderType.COMPANION)
        self.assertEqual(row["tag"], "老板指定打手")
        # 默认档也是真值：没填订单类型 ⇒ 空串（模型 `blank=True`，⛔ 不编一个枚举）。
        self._order("20261028000000000602")
        self.assertEqual(self._row("20261028000000000602")["order_type"], "")

    def test_the_platform_placeholder_taker_is_flagged_true(self):
        """⭐ 2026-10-28（业务方）：占位接手人的单 ⇒ `taker_is_platform=True`。

        判据是**固定 username**（`dailiantong_push.VIRTUAL_TAKER_USERNAME`），⛔ 不是
        「名字串等于『代练通』」——显示名会随业务口径改（本线就从「代练通打手」改成
        「代练通」），按串判会在旧名存量上静默漏。同时钉住那格显示值 = 新名字。
        """
        order = self._order(self.NO)
        placeholder = placeholder_taker_user()
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=placeholder
        )

        row = self._row(self.NO)
        self.assertIs(row["taker_is_platform"], True)
        self.assertEqual(row["taker_name"], "代练通")

    def test_a_real_taker_is_not_flagged(self):
        """真接手人 ⇒ `taker_is_platform=False`（同一把尺子的另一档，防「恒 True」的假绿）。"""
        order = self._order(self.NO)
        real = User.objects.create_user(username="cols-real-taker")
        Performance.objects.create(order=order, emp_type=EmpType.MAIN, user=real)

        row = self._row(self.NO)
        self.assertIs(row["taker_is_platform"], False)
        self.assertEqual(row["taker_name"], "cols-real-taker")


