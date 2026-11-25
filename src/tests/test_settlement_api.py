"""P4-8：三个**只读**接口 + `scope_queryset` 接线（spec §6.1 / §7；brief 的测试清单）。

三个接口（spec §7 的端点表）：

| 路径 | 方法 | 谁 | 要点 |
|---|---|---|---|
| `/api/business/settlements` | GET | 财务 / 客服 | 筛：人 / 时间窗 / `review_status` / `settle_status` / `emp_type` / 订单号；**每行带 `payable_net`** |
| `/api/business/performances/{id}/adjustments` | GET | 财务 / 客服 | 该绩效的调整记录链（含 `settle_status`） |
| `/api/business/settlements/unattributed` | GET | 财务 / 客服 | 已 `COMPLETED` 但**没有销售提成行**的订单（spec §3.8） |

D-0089 另加一条同域的只读端点 `GET /api/business/settlements/people`（「结账对象人员」
下拉的候选：**数据里出现过的人**，可按游戏收窄）——它不是 spec §7 那张表的成员，
测试在 `SettlementPeopleTests`；跨租户隔离由 `tests/test_p4_isolation.py` 钉。

八条纪律（每条都有会红的用例）：

1. **筛选取向逐条各一条**（人 / 时间窗 / 审核状态 / 结账状态 / 收入类型 / 订单号）。
2. **边界值**：`?emp_type=` 空串按「**未传**」（P2 起的既有口径）——不是非法值、也不筛出空集；
   非法值 ⇒ **200 + 业务码**，不是 HTML 500（E-0010）。
3. ⚠️ **`?page=abc` 的实际码是 `1002`（不是 `1000`）**：DRF 分页器抛 `NotFound`，
   `apps/core/exceptions.py` 把它映射成 `BUSINESS`（P3b B1-4 实测踩到，本任务**沿用该实测值**）。
4. ⭐ **`payable_net` 是接线断言**：断言**列表行里**的数等于手算值
   （`test_settlement_amount.py` 已单测过函数本身，这里测的是「真的被接进列表」）。
   手算口径（spec §4.4 ③）= `bonus_amount − 扣点额 + Σ(未结账调整记录)`——
   式子里同时放一条**已结账**的调整记录，它**不得**被计入。
5. **负的 `payable_net` 照实返回**（D-0015：不拒绝、不置 0、不加 `≥ 0`）。
6. ⭐ **「待归属」的判据是「没有销售提成行」，不是「`sales_user` 为空」**（spec §3.8）：
   所以正向用例故意造一条 **`sales_user` 有值、却没有销售提成行**的订单——两个判据在它身上**不等价**，
   变异成 `sales_user__isnull=True` 时它会立刻从可见变不可见（见报告的变异表 M2）。
   **先证明有数据**再下否定断言（`assertNotIn` 之前先 `assertIn`），⛔ 不用 `count()` 做否定断言。
7. ⭐ **`scope_queryset` 接线判别力**（照 P3a 的 `ScopingWiringTests`）：桩打在**返回值**上（清空），
   并配**不接桩的对照**——「断言它被调用」会漏掉「调用了却丢掉返回值」那种写法。
8. **越权用例一律用非管理员账号**（§9 #3）：`has_node_now` 对 `is_superuser` 有**短路**
   （平面守卫排在它前面），拿管理员测闸门会恒放行。

⚠️ 期望值一律写**字面值**（E-0024）：业务码写 `1000` / `1002`，金额写 `"12.00"` / `"-15.00"`，
状态写 `"已结账"` / `"未结账"`——不引被测模块导出的常量（自指断言不会红）。
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Group, User
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderSource,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    ReviewStatus,
    SettleStatus,
)
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode, Role
from tests.p5_auth_helpers import make_logged_in_client

#: spec §7 的三个路径（**没有**尾斜杠；与 P4-7 的 `/settlements/settle` 同形）。
SETTLEMENTS_URL = "/api/business/settlements"
UNATTRIBUTED_URL = "/api/business/settlements/unattributed"
#: D-0089 的「结账对象人员」候选（同样**没有**尾斜杠）。
PEOPLE_URL = "/api/business/settlements/people"
#: 三个闸门共用的节点 code——`apps/rbac/catalog.py` 里 `benchmark_id = 80` / `code = "finance.payroll"`
#: 那一行（⚠️ **不写行号**：目录裁剪会让它漂，按 id/code 指认），对标 **80「打手与客服工资」**。
NODE_CODE = "finance.payroll"


def adjustments_url(performance_id):
    return f"/api/business/performances/{performance_id}/adjustments"


class _P4ReadFixture(TenantTestCase):
    """建单 / 建绩效 / 建调整记录的小夹具（各用例只表达**自己的差异**）。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # 业务逻辑用例用管理员：只是为了**跳过建角色目录**（"谁被拦"由
        # `SettlementReadPermissionTests` 用非管理员双向钉住）。
        self.finance = make_logged_in_client(username="finance-p4-8", is_superuser=True)
        self.taker_a = User.objects.create_user(username="taker-a")
        self.taker_b = User.objects.create_user(username="taker-b")
        self.sales_a = User.objects.create_user(username="sales-a")
        self._order_seq = 0

    def _order(self, *, status=OrderStatus.COMPLETED, sales_user=None, hall_price="100.00", game=None):
        self._order_seq += 1
        return Order.objects.create(
            game=game or self.game,
            order_no=f"2026092000000{self._order_seq:07d}",
            status=status,
            hall_price=Decimal(hall_price),
            sales_user=sales_user,
        )

    def _perf(self, order, user=None, **overrides):
        fields = {
            "emp_type": EmpType.MAIN,
            "temp_amount": Decimal("20.00"),
            "bonus_amount": Decimal("20.00"),
            "commission_rate": Decimal("10.00"),
            "deduct_mode": "",
            "deduct_value": Decimal("0.00"),
            "review_status": ReviewStatus.APPROVED,
            "settle_status": SettleStatus.UNSETTLED,
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, user=user, **fields)

    def _adj(self, performance, amount, *, settle=SettleStatus.UNSETTLED):
        return PerformanceAdjustment.objects.create(
            performance=performance,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal(amount),
            reason="测试调整",
            operator="fin",
            settle_status=settle,
        )

    def _get(self, url, params=None, client=None):
        return (client or self.finance).get(url, params or {})

    def _items(self, url, params=None, client=None):
        body = self._get(url, params, client).json()
        # 前置哨兵：端点本身成功。失败用例不走这个 helper（否则报错位置会误导）。
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]


class SettlementListTests(_P4ReadFixture):
    """`GET /api/business/settlements`：分页形状 + 六条筛选 + 边界值。"""

    def test_the_list_has_the_p2_pagination_shape(self):
        self._perf(self._order(), self.taker_a)

        body = self._get(SETTLEMENTS_URL).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(
            set(body["data"]), {"items", "total", "page", "page_size"}
        )
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["page_size"], 20)

    def test_a_row_carries_the_display_name_beside_the_login_account(self):
        """列表行里两个键**语义不同、都要在**（2026-10-03 display-fixes 线）：

        - `display_name` = 显示名（昵称优先、空串回落登录账号，口径同 `/settlements/people`）；
        - `username` = **登录账号**（既有契约，保留）。

        ⚠️ 两档都断，且昵称与账号**不同形** ⇒ 「实现其实没加 display_name」或
        「display_name 还是账号名」两种错法都会红。
        """
        self.taker_a.first_name = "测试昵称甲"
        self.taker_a.save()
        self._perf(self._order(), user=self.taker_a)
        self._perf(self._order(), user=self.taker_b)

        rows = {row["username"]: row for row in self._items(SETTLEMENTS_URL)}

        self.assertEqual(sorted(rows), ["taker-a", "taker-b"])
        # 昵称档
        self.assertEqual(rows["taker-a"]["display_name"], "测试昵称甲")
        self.assertNotEqual(rows["taker-a"]["display_name"], rows["taker-a"]["username"])
        # 回落档（`taker_b` 的 `first_name` 是空串）
        self.assertEqual(rows["taker-b"]["display_name"], "taker-b")
        # `username` 仍是登录账号本身
        self.assertEqual(rows["taker-a"]["username"], "taker-a")

    def test_a_row_carries_the_source_order_no_beside_the_system_order_no(self):
        """⭐ 2026-10-09：结算记录页的「关联订单」列要显示**来源单号** ⇒ 出参必须有它。

        业务方原话：「列表的关联订单号采用的是系统内置单号、这里关联的采用应该是来源单号」。
        实测依据（对标字段语义）见 `.tools/recon/order-view/_SOURCE.txt:14-19`
        （`jionOrder` = 19 位来源单号、`orderId` = 20 位系统单号）。

        ⚠️ **两个键都要在、且值必须不同形**：只断 `source_order_no` 存在的话，
        「实现其实是把 `order_no` 抄了两遍」这种错法**照样绿**（自指/同源断言）。
        """
        order = self._order()
        order.source_order_no = "TB-合成-结算-来源甲"
        order.save(update_fields=["source_order_no"])
        self._perf(order, user=self.taker_a)

        row = self._items(SETTLEMENTS_URL)[0]

        self.assertEqual(row["source_order_no"], "TB-合成-结算-来源甲")
        self.assertEqual(row["order_no"], order.order_no)
        self.assertNotEqual(row["source_order_no"], row["order_no"])

    def test_a_blank_source_order_no_is_an_empty_string_not_null(self):
        """没填来源单号 ⇒ **空串**（`Order.source_order_no` 是 `blank=True` 非 NULL 的文本列）。

        ⚠️ 这条是前端「空就渲染空、**不回落**成系统单号」那一条判据的**后端前提**：
        后端若在这里回 `null`，前端的 `?? ''` 会把它吞掉、判据就分不出两回事了。
        """
        self._perf(self._order(), user=self.taker_a)

        row = self._items(SETTLEMENTS_URL)[0]

        self.assertEqual(row["source_order_no"], "")
        self.assertIsNotNone(row["source_order_no"])

    def test_filters_by_user(self):
        first = self._perf(self._order(), self.taker_a)
        second = self._perf(self._order(), self.taker_b)

        self.assertEqual(
            [r["id"] for r in self._items(SETTLEMENTS_URL, {"user": self.taker_a.id})],
            [first.id],
        )
        self.assertEqual(
            [r["id"] for r in self._items(SETTLEMENTS_URL, {"user": self.taker_b.id})],
            [second.id],
        )

    def test_filters_by_date_range(self):
        """时间窗按**结账时间**（`settled_at`）的本地日期收窄，两端都是**含**的。

        ⚠️ **不是创建时间**：本页这一列筛的是对标「结账时间」⇒ `settled_at`（结账那一刻写入）
        才是被筛的列，而 `settled_at is None`（未结账）的行**天然被排除**
        （判别力由下一条 `test_date_window_excludes_unsettled_rows_created_in_range` 钉住）。
        两条绩效被显式改到「3 天前结账」与「今天结账」——单向取值（`date_from` 或 `date_to`）
        都能分辨，不会出现「两边都全中」的假绿。
        """
        old = self._perf(self._order(), self.taker_a)
        today = self._perf(self._order(), self.taker_b)
        three_days_ago = timezone.localdate() - timedelta(days=3)
        # 只拉开**结账时间**：`created_at` 在本口径里不参与（下一用例证明它单独不够）。
        Performance.objects.filter(pk=old.pk).update(
            settled_at=timezone.now() - timedelta(days=3)
        )
        # `today` 必须**也**已结账：未结账的行在本口径下不进列表（默认 `settled_at=None`）。
        Performance.objects.filter(pk=today.pk).update(settled_at=timezone.now())

        self.assertEqual(
            sorted(
                r["id"]
                for r in self._items(
                    SETTLEMENTS_URL, {"date_from": three_days_ago.isoformat()}
                )
            ),
            sorted([old.id, today.id]),
        )
        self.assertEqual(
            [
                r["id"]
                for r in self._items(
                    SETTLEMENTS_URL,
                    {"date_to": (three_days_ago - timedelta(days=1)).isoformat()},
                )
            ],
            [],
        )
        self.assertEqual(
            [
                r["id"]
                for r in self._items(
                    SETTLEMENTS_URL, {"date_to": three_days_ago.isoformat()}
                )
            ],
            [old.id],
        )
        self.assertEqual(
            [
                r["id"]
                for r in self._items(
                    SETTLEMENTS_URL,
                    {"date_from": (three_days_ago + timedelta(days=1)).isoformat()},
                )
            ],
            [today.id],
        )

    def test_date_window_excludes_unsettled_rows_created_in_range(self):
        """⭐ 本次修复的**核心判别力用例**：时间窗筛的是结账时间（`settled_at`），不是创建时间。

        两行的 `created_at` **都**用 `timezone.now()` 造在「今天」这个区间内，唯一差别是有没有结账：

        | 行 | `created_at` | `settled_at` | 期望 |
        |---|---|---|---|
        | `settled` | 今天 | 今天 | ✅ 收进 |
        | `unsettled` | 今天 | `None`（未结账） | ⛔ **必须排除** |

        ⇒ 若把筛选改回 `created_at__date__gte/lte`（旧实现），两行的创建日期都在区间内
        ⇒ `unsettled` 也出现 ⇒ 本断言**变红**（这就是它的判别力，见报告里的变异自证）。
        """
        now = timezone.now()
        settled = self._perf(self._order(), self.taker_a)
        unsettled = self._perf(self._order(), self.taker_b)
        # 两行的创建时间显式落在「今天」（不依赖 `auto_now_add` 的隐式值）。
        Performance.objects.filter(pk=settled.pk).update(created_at=now, settled_at=now)
        Performance.objects.filter(pk=unsettled.pk).update(created_at=now)

        today = timezone.localdate(now).isoformat()
        rows = self._items(SETTLEMENTS_URL, {"date_from": today, "date_to": today})

        self.assertEqual([r["id"] for r in rows], [settled.id])

    def test_filters_by_review_status(self):
        pending = self._perf(
            self._order(), self.taker_a, review_status=ReviewStatus.PENDING
        )
        approved = self._perf(
            self._order(), self.taker_b, review_status=ReviewStatus.APPROVED
        )

        self.assertEqual(
            [
                r["id"]
                for r in self._items(
                    SETTLEMENTS_URL, {"review_status": ReviewStatus.PENDING}
                )
            ],
            [pending.id],
        )
        self.assertEqual(
            [
                r["id"]
                for r in self._items(
                    SETTLEMENTS_URL, {"review_status": ReviewStatus.APPROVED}
                )
            ],
            [approved.id],
        )

    def test_filters_by_settle_status(self):
        settled = self._perf(
            self._order(), self.taker_a, settle_status=SettleStatus.SETTLED
        )
        unsettled = self._perf(self._order(), self.taker_b)

        self.assertEqual(
            [
                r["id"]
                for r in self._items(
                    SETTLEMENTS_URL, {"settle_status": SettleStatus.SETTLED}
                )
            ],
            [settled.id],
        )
        self.assertEqual(
            [
                r["id"]
                for r in self._items(
                    SETTLEMENTS_URL, {"settle_status": SettleStatus.UNSETTLED}
                )
            ],
            [unsettled.id],
        )

    def test_filters_by_emp_type(self):
        main = self._perf(self._order(), self.taker_a, emp_type=EmpType.MAIN)
        sales = self._perf(self._order(), self.sales_a, emp_type=EmpType.SALES)

        self.assertEqual(
            [r["id"] for r in self._items(SETTLEMENTS_URL, {"emp_type": EmpType.MAIN})],
            [main.id],
        )
        self.assertEqual(
            [r["id"] for r in self._items(SETTLEMENTS_URL, {"emp_type": EmpType.SALES})],
            [sales.id],
        )

    def test_filters_by_order_no(self):
        first = self._perf(self._order(), self.taker_a)
        second = self._perf(self._order(), self.taker_b)

        self.assertEqual(
            [
                r["id"]
                for r in self._items(
                    SETTLEMENTS_URL, {"order_no": first.order.order_no}
                )
            ],
            [first.id],
        )
        # 反向：另一个订单号只命中另一条（防止「筛选被忽略、一律返回全部」的假绿）
        self.assertEqual(
            [
                r["id"]
                for r in self._items(
                    SETTLEMENTS_URL, {"order_no": second.order.order_no}
                )
            ],
            [second.id],
        )

    def test_game_filter_narrows_to_that_game(self):
        """`?game=<本游戏>` 回本游戏的行，`?game=<另一游戏>` 回另一游戏的行。"""
        other = Game.objects.create(name="绝区零")
        mine = self._perf(self._order(), self.taker_a)
        theirs = self._perf(self._order(game=other), self.taker_b)
        self.assertEqual(
            [r["id"] for r in self._items(SETTLEMENTS_URL, {"game": self.game.id})],
            [mine.id],
        )
        self.assertEqual(
            [r["id"] for r in self._items(SETTLEMENTS_URL, {"game": other.id})],
            [theirs.id],
        )

    def test_game_absent_returns_all_games(self):
        """不传 `game` = 全量（两面都回）。"""
        other = Game.objects.create(name="绝区零")
        mine = self._perf(self._order(), self.taker_a)
        theirs = self._perf(self._order(game=other), self.taker_b)
        self.assertEqual(
            sorted(r["id"] for r in self._items(SETTLEMENTS_URL)),
            sorted([mine.id, theirs.id]),
        )

    def test_game_zero_is_rejected_with_code_1000(self):
        """`?game=0` ⇒ 业务码 1000（**不是** 500、**不是**静默全量）。"""
        body = self._get(SETTLEMENTS_URL, {"game": 0}).json()
        self.assertEqual(body["code"], 1000)

    def test_game_non_integer_is_rejected_with_code_1000(self):
        body = self._get(SETTLEMENTS_URL, {"game": "abc"}).json()
        self.assertEqual(body["code"], 1000)

    def test_empty_string_filters_are_treated_as_not_provided(self):
        """前端「没选」常送 `?emp_type=&user=&order_no=`——那是**没传**，不是非法值，也不筛空集。

        ⚠️ 边界判据是 `v != ""` 的入口过滤（**不是** `is not None` 的真值判断，E-0021）：
        去掉它，`emp_type=""` 会进 `ChoiceField` 被判非法，本用例立刻拿到 `1000` 而不是 `0`。
        """
        main = self._perf(self._order(), self.taker_a)
        sales = self._perf(self._order(), self.taker_b, emp_type=EmpType.SALES)
        blank = {
            "user": "",
            "emp_type": "",
            "review_status": "",
            "settle_status": "",
            "date_from": "",
            "date_to": "",
            "order_no": "",
        }

        self.assertEqual(
            sorted(r["id"] for r in self._items(SETTLEMENTS_URL, blank)),
            sorted([main.id, sales.id]),
        )
        # 对照：非空值**确实在筛**（否则「空串当没传」会退化成「筛选整体失效」也绿）
        self.assertEqual(
            [r["id"] for r in self._items(SETTLEMENTS_URL, {"emp_type": EmpType.MAIN})],
            [main.id],
        )

    def test_malformed_filters_are_business_codes_not_500(self):
        """请求来源的值一律过序列化器（E-0010）。

        ⚠️ `?page=abc` 走的是**另一条**分支：DRF 分页器抛 `NotFound`，而
        `apps/core/exceptions.py` 把 `NotFound` 映射成 **`BUSINESS`(1002)**，不是 `VALIDATION`。
        期望值取自 P3b B1-4 的**实测**（第一版写 1000，实测 1002）。
        """
        self._perf(self._order(), self.taker_a)
        cases = [
            ({"emp_type": "BOGUS"}, 1000),
            ({"review_status": "BOGUS"}, 1000),
            ({"settle_status": "BOGUS"}, 1000),
            ({"user": "abc"}, 1000),
            ({"user": "0"}, 1000),          # `min_value=1`：入口就拒
            ({"date_from": "garbage"}, 1000),
            # ⚠️ D-0301：`order_no` 精确框放宽到双列（来源号列宽 128）⇒
            # 「21 位」不再超宽；本条的**超列宽**样本换成 129 位（= 128 + 1）。
            ({"order_no": "9" * 129}, 1000),
            ({"page": "abc"}, 1002),         # 分页器的 NotFound 分支
        ]
        for params, expected in cases:
            with self.subTest(params=params):
                response = self._get(SETTLEMENTS_URL, params)
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["code"], expected)
                self.assertIsNone(body["data"])


class SettlementOrderSourceFilterTests(_P4ReadFixture):
    """⭐ D-0323（2026-10-29）：`order_source` **两态**筛选（给来源分解行的「查看明细」用）。

    三态：正整数 ⇒ `order__order_source_id=<id>`；字面 `'none'` ⇒ 未指定来源
    （`order__order_source__isnull=True`）；不传 / 空串 ⇒ 不筛（全量）。
    两态语义与校验的唯一出处 = `SettlementSearchSerializer.validate_order_source`。
    """

    def _order(self, *, order_source=None, **kwargs):
        """基类夹具不认 `order_source`（本线新维度）⇒ 建单后补写 FK 再返回。"""
        order = super()._order(**kwargs)
        if order_source is not None:
            order.order_source = order_source
            order.save(update_fields=["order_source"])
        return order

    def setUp(self):
        super().setUp()
        self.src_main = OrderSource.objects.create(name="甲店铺")
        self.src_other = OrderSource.objects.create(name="乙店铺")
        # 三个来源档各一张已结账单：甲 / 乙 / 未指定。
        self.perf_main = self._perf(
            self._order(order_source=self.src_main), self.taker_a
        )
        self.perf_other = self._perf(
            self._order(order_source=self.src_other), self.taker_b
        )
        self.perf_none = self._perf(self._order(), self.taker_a)

    def test_integer_filter_keeps_only_that_source(self):
        rows = self._items(SETTLEMENTS_URL, {"order_source": self.src_main.pk})
        self.assertEqual([r["id"] for r in rows], [self.perf_main.id])

    def test_none_literal_keeps_only_unspecified_orders(self):
        rows = self._items(SETTLEMENTS_URL, {"order_source": "none"})
        self.assertEqual([r["id"] for r in rows], [self.perf_none.id])

    def test_not_provided_returns_all(self):
        """不传 = 全量；空串按「没传」（与既有筛选同口径）。"""
        self.assertEqual(
            len(self._items(SETTLEMENTS_URL)),
            Performance.objects.count(),
        )
        self.assertEqual(
            len(self._items(SETTLEMENTS_URL, {"order_source": ""})),
            Performance.objects.count(),
        )

    def test_malformed_values_are_business_codes_not_500(self):
        """0 / 负数 / 非数字 / 别的词 ⇒ 200 + `1000`（E-0010，不是 HTML 500、不是静默全量）。

        ⚠️ `"²"`（E-0299，评审 M1）：`str.isdigit()` 对 Unicode 上标返回 True 而
        `int('²')` 抛 `ValueError`——修复前该值穿出 `is_valid()` 与
        `envelope_exception_handler` 变 **HTML 500**，本条就是那次的钉子
        （断言显式含 `status_code == 200`）。全角数字 `'１'` 这类 `int()` 吃得下的
        **照旧算合法数字串**，不收紧（别过度）。
        """
        for bad in ("0", "-1", "abc", "NULL", "²"):
            with self.subTest(bad=bad):
                response = self._get(SETTLEMENTS_URL, {"order_source": bad})
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["code"], 1000)
                self.assertIsNone(body["data"])


class SettlementPayableNetTests(_P4ReadFixture):
    """⭐ brief 第 4/5 条：`payable_net` **出现在列表行里**且等于手算值；负值照实返回。"""

    def _row_of(self, performance_id):
        rows = self._items(SETTLEMENTS_URL)
        return next(r for r in rows if r["id"] == performance_id)

    def test_payable_net_equals_bonus_minus_deduction_plus_unsettled_adjustments(self):
        """手算：`20.00（bonus） − 5.00（固定扣点） + (−3.00（未结账调整）) = 12.00`。

        ⚠️ 那条 **已结账** 的 `−100.00` 调整记录**不得**被计入（spec §4.4 ③ 只加「尚未结账」的）：
        漏掉这个过滤，本用例会拿到 `−88.00`。
        ⚠️ 固定扣点的 `deduct_value=5.00 ≤ temp_amount=20.00`，是合法区间（不比出 `AmountError`）。
        """
        perf = self._perf(
            self._order(),
            self.taker_a,
            temp_amount=Decimal("20.00"),
            bonus_amount=Decimal("20.00"),
            deduct_mode="1",
            deduct_value=Decimal("5.00"),
        )
        self._adj(perf, "-3.00")
        self._adj(perf, "-100.00", settle=SettleStatus.SETTLED)

        row = self._row_of(perf.id)

        self.assertEqual(row["payable_net"], "12.00")            # 钉字面值（含两位小数格式）
        self.assertEqual(Decimal(row["payable_net"]), Decimal("12.00"))
        self.assertEqual(Decimal(row["bonus_amount"]), Decimal("20.00"))

    def test_a_negative_payable_net_is_returned_as_is(self):
        """亏损单：`10.00 + (−25.00) = −15.00`——**照实返回**，不拒绝、不置 0（D-0015）。

        ⚠️ 拒绝结账是 P4-7 的 `1011`（那在 `POST /settlements/settle` 上），
        列表**只读**，它没有资格替财务决定「不显示」。
        """
        perf = self._perf(
            self._order(), self.taker_a, bonus_amount=Decimal("10.00")
        )
        self._adj(perf, "-25.00")

        row = self._row_of(perf.id)

        self.assertEqual(row["payable_net"], "-15.00")

    def test_a_row_without_adjustments_reports_the_bare_bonus(self):
        """对照：没有调整记录、没有扣点时，净额就是 `bonus_amount` 本身。"""
        perf = self._perf(
            self._order(), self.taker_a, bonus_amount=Decimal("17.60")
        )

        self.assertEqual(self._row_of(perf.id)["payable_net"], "17.60")

    def test_an_uncomputable_deduction_is_a_business_code_not_a_500(self):
        """`payable_net` **会透传 `AmountError`**（`settlement_amount.py` 的 docstring 明写）：
        逐行调用时必须预期这个失败形态，按业务码翻译——**不是**「净额 0」，更不是 HTML 500。

        `deduct_value=500.00 > temp_amount=20.00` ⇒ `deduct_amount` 抛 `AmountError`
        （扣点金额不能超过预计绩效总计）。这种行可以由历史数据 / 直接写库产生
        （`/adjust` 当时的校验基于**当时**的 `temp_amount`）。
        """
        self._perf(
            self._order(),
            self.taker_a,
            temp_amount=Decimal("20.00"),
            deduct_mode="1",
            deduct_value=Decimal("500.00"),
        )

        response = self._get(SETTLEMENTS_URL)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 1000)
        self.assertIn("扣点", body["msg"])
        self.assertIsNone(body["data"])

    def test_the_rejection_message_identifies_the_bad_row(self):
        """⭐ 清单 9（P4-8 评审 Minor 1）：整表拒绝是对的，但 `msg` 必须指出**是哪一行**。

        判据有两半，缺一不可：

        1. `msg` 里含**出错的绩效 id**（`performance_id`）与它的**订单号**——
           列表可能几百行，只报「有一行坏」等于让财务去猜。
        2. `msg` 里含**订单号**这条业务标识（而不是只有主键）：主键只在租户内有意义。
        形状不变：`code` 仍是 `1000`、`data` 仍是 `None`（整表拒绝）。
        """
        bad = self._perf(
            self._order(),
            self.taker_a,
            temp_amount=Decimal("20.00"),
            deduct_mode="1",
            deduct_value=Decimal("500.00"),
        )

        body = self._get(SETTLEMENTS_URL).json()

        self.assertEqual(body["code"], 1000)
        self.assertIn(str(bad.id), body["msg"])              # 出错行的绩效 id
        self.assertIn(bad.order.order_no, body["msg"])       # 出错行的订单号
        self.assertIsNone(body["data"])

    def test_a_good_row_alone_does_not_get_a_row_marker(self):
        """对照：没有坏行时**不出现**那条行标识（否则「标记」会恒在、上面那条判据就没有判别力）。"""
        perf = self._perf(
            self._order(),
            self.taker_a,
            temp_amount=Decimal("20.00"),
            bonus_amount=Decimal("20.00"),
            deduct_mode="1",
            deduct_value=Decimal("5.00"),
        )

        body = self._get(SETTLEMENTS_URL).json()

        self.assertEqual(body["code"], 0)
        self.assertNotIn("（绩效", body["msg"])


class SettlementUnattributedTests(_P4ReadFixture):
    """⭐ 「待归属」：已 `COMPLETED` 但**没有销售提成行**的订单（spec §3.8）。

    ⚠️ 判据是**行**，不是 `sales_user` 字段：`sales_user` 有值但客服行没建出来
    （录单时归属人已填、`create_customer_performance` 没跑/被清理）同样算「待归属」。
    两个判据**不等价**——正向用例故意带 `sales_user`，变异成 `sales_user__isnull=True` 时它必须红。
    """

    def test_a_completed_order_without_a_sales_row_is_visible(self):
        """正向：`COMPLETED` + `sales_user` **有值** + **没有**销售提成行 ⇒ 必须可见。"""
        order = self._order(sales_user=self.sales_a)
        self._perf(order, self.taker_a, emp_type=EmpType.MAIN)

        nos = [r["order_no"] for r in self._items(UNATTRIBUTED_URL)]

        self.assertIn(order.order_no, nos)               # 先证明有数据
        self.assertEqual(nos, [order.order_no])

    def test_a_completed_order_with_a_sales_row_is_hidden(self):
        """反向：`COMPLETED` + **有**销售提成行 ⇒ 不可见（它已经归属到人，财务不必处理）。"""
        attributable = self._order(sales_user=self.sales_a)
        self._perf(attributable, self.sales_a, emp_type=EmpType.SALES)
        unattributed = self._order(sales_user=self.sales_a)
        self._perf(unattributed, self.taker_a, emp_type=EmpType.MAIN)

        nos = [r["order_no"] for r in self._items(UNATTRIBUTED_URL)]

        self.assertIn(unattributed.order_no, nos)        # 先证明有数据（否则否定断言恒真）
        self.assertNotIn(attributable.order_no, nos)

    def test_an_unfinished_order_without_a_sales_row_is_hidden(self):
        """`status != COMPLETED` 的一律不列（还没确定，不是「待归属」）。"""
        completed = self._order(sales_user=self.sales_a)
        self._perf(completed, self.taker_a)
        in_progress = self._order(status=OrderStatus.IN_PROGRESS, sales_user=self.sales_a)
        self._perf(in_progress, self.taker_a)

        nos = [r["order_no"] for r in self._items(UNATTRIBUTED_URL)]

        self.assertIn(completed.order_no, nos)
        self.assertNotIn(in_progress.order_no, nos)

    def test_the_list_has_the_p2_pagination_shape(self):
        order = self._order(sales_user=self.sales_a)
        self._perf(order, self.taker_a)

        body = self._get(UNATTRIBUTED_URL).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(set(body["data"]), {"items", "total", "page", "page_size"})
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["items"][0]["order_no"], order.order_no)



    def test_a_hall_shadow_order_is_never_unattributed(self):
        """⭐ 评审 Important 6：隐藏单（镜像卡派生的载体）不是「已完成的真单」⇒ 不进待归属。"""
        from tests.hall_shadow_helpers import make_hidden_order, make_mirror

        normal = self._order(sales_user=self.sales_a)
        self._perf(normal, self.taker_a)
        card = make_mirror(
            game=self.game, bound_by=self.sales_a, no="SYN-SHADOW-UNATTR"
        )
        hidden = make_hidden_order(
            game=self.game, card=card, order_no="20260920000000000901",
            status=OrderStatus.COMPLETED,
        )
        self._perf(hidden, self.taker_a)

        nos = [row["order_no"] for row in self._items(UNATTRIBUTED_URL)]

        self.assertIn(normal.order_no, nos)                # 先证明有数据
        self.assertNotIn(hidden.order_no, nos)


class SettlementPeopleTests(_P4ReadFixture):
    """`GET /settlements/people`（D-0089）：候选 = **在结算数据里出现过的人**。

    ⚠️ 与「结算列表筛选」是**两件事**：本端点回答「这个下拉里能选谁」，
    所以它有两条与列表相反的取向——**不按 `is_active` 过滤**（停用的人的历史绩效仍要能筛）、
    **去重到「人」**（同一个人多少行绩效都只出现一次）。
    """

    def _people(self, params=None, client=None):
        body = self._get(PEOPLE_URL, params, client).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]

    def test_returns_distinct_usernames_sorted(self):
        """同一个人多行只出现一次；顺序按用户名升序（**断言字面值序列**）。

        ⭐ 这条是本端点**唯一**能抓到「`values(...).distinct()` 继承 `Performance.Meta.ordering`
        （`["-created_at", "-id"]`）」的用例：Django 会把那两列加进 SELECT 列表兜住 ORDER BY
        ⇒ `DISTINCT` 落在 `(user_id, username, created_at, id)` 上 ⇒ 同一个人的 2 行绩效
        变成 2 条候选。断言**整个列表的字面值**（而不是 `assertIn` / `len(...) >= 1`），
        漏掉显式 `order_by` 时这里实测拿到 `['aaa-first', 'taker-a', 'taker-a']`（见报告的变异 M1）。
        """
        self.taker_b.username = "aaa-first"
        self.taker_b.save()
        for _ in range(2):
            self._perf(self._order(), user=self.taker_a)   # 两次同一人
        self._perf(self._order(), user=self.taker_b)
        self.assertEqual(
            [p["username"] for p in self._people()],
            ["aaa-first", "taker-a"],
        )

    def test_excludes_rows_without_user(self):
        """`user is null` 的行**不产生**候选。

        ⚠️ **`review_status` 必须一起给 `None`**（实际夹具的约束，不是判据放宽）：
        `Performance` 有 DB 约束 `performance_review_needs_user`
        （`review_status IS NULL OR user IS NOT NULL`，`models.py` 第 ③ 条）——
        「没有归属人就不可能有审核状态」。用 `_perf` 的默认 `review_status=APPROVED` 造
        `user=None` 的行会撞约束抛 `IntegrityError` ⇒ 用例变 **ERROR**（不是有效的 RED）。
        断言本身一个字没改：这行仍然是 `user=None` 的绩效行。
        """
        self._perf(self._order(), user=self.taker_a)
        self._perf(self._order(), user=None, review_status=None)
        self.assertEqual([p["username"] for p in self._people()], ["taker-a"])

    def test_includes_inactive_accounts(self):
        """⚠️ **不按 `is_active` 过滤**：数据里出现过的人，停用了也要能按人筛（口径钉死）。"""
        self.taker_a.is_active = False
        self.taker_a.save()
        self._perf(self._order(), user=self.taker_a)
        self.assertEqual([p["username"] for p in self._people()], ["taker-a"])

    def test_game_narrows_candidates(self):
        """按游戏收窄：另一游戏的绩效行上的人不出现。"""
        other = Game.objects.create(name="绝区零")
        self._perf(self._order(), user=self.taker_a)
        self._perf(self._order(game=other), user=self.taker_b)
        self.assertEqual(
            [p["username"] for p in self._people({"game": self.game.id})],
            ["taker-a"],
        )

    def test_game_zero_is_rejected_with_code_1000(self):
        body = self._get(PEOPLE_URL, {"game": 0}).json()
        self.assertEqual(body["code"], 1000)

    def test_game_non_integer_is_rejected_with_code_1000(self):
        """`?game=abc` ⇒ 业务码 1000（**不是** 500）——照结算列表那条同款（M-2）。

        ⚠️ 与 `?game=0` 是**两条不同的入口校验**：`0` 撞 `min_value=1`，
        非数字撞 `IntegerField` 的类型解析；只测其中一条会漏掉另一条。
        """
        body = self._get(PEOPLE_URL, {"game": "abc"}).json()
        self.assertEqual(body["code"], 1000)

    def test_anonymous_is_401_not_403(self):
        """未认证 ⇒ **401**（与既有 `test_anonymous_is_401_not_403` 同一写法）。

        ⚠️ **必须照既有那条的写法**（本文件 `SettlementReadPermissionTests` 里那条）：
        用 `Client(SERVER_NAME="tenant.test.com")`，⛔ **不是** `self.client.get(...)`——
        后者不带租户域，命不中租户 schema，断言 401 会以别的原因通过/失败。
        """
        anonymous = Client(SERVER_NAME="tenant.test.com")
        self.assertEqual(anonymous.get(PEOPLE_URL).status_code, 401)

    def test_returns_id_username_and_display_name_only(self):
        """出参**恰好**三把键：`id` / `username` / `display_name`，不夹带手机号/姓名之类的档案字段。

        ⚠️ display-fixes 线）由「只有两把键」改准：`display_name` 是**加法的**
        新键（昵称优先、空串回落登录账号），`username`（登录账号）**保留**。
        ⛔ 判据仍是**恰好集合**，不是 `assertIn` / `assertTrue(... in keys)`。
        """
        self._perf(self._order(), user=self.taker_a)
        self.assertEqual(
            sorted(self._people()[0].keys()), ["display_name", "id", "username"]
        )

    def test_display_name_prefers_the_nickname_and_falls_back_to_the_login_account(self):
        """显示名两档都断：`first_name` 是昵称 ⇒ `display_name` 是昵称；`first_name` 是**空串**
        ⇒ `display_name` 是该人的 `username`（口径唯一出处 `apps/accounts/display.py::user_display_name`）。

        ⚠️ 夹具的两个账号都取名（`taker-a` / `taker-b`），所以 `display_name` 与 `username`
        **不同形**的那一档才抓得到「实现其实还是取了账号名」。
        """
        self.taker_a.first_name = "测试昵称甲"
        self.taker_a.save()
        self._perf(self._order(), user=self.taker_a)
        # `taker_b` 建号时没给 `first_name` ⇒ 默认空串，正是「回落」那一档。
        self._perf(self._order(), user=self.taker_b)

        rows = {row["username"]: row for row in self._people()}

        self.assertEqual(sorted(rows), ["taker-a", "taker-b"])
        # 昵称档
        self.assertEqual(rows["taker-a"]["display_name"], "测试昵称甲")
        self.assertNotEqual(rows["taker-a"]["display_name"], rows["taker-a"]["username"])
        # 回落档（昵称是空串，⛔ 不是「空白串也算空」）
        self.assertEqual(rows["taker-b"]["display_name"], "taker-b")

    def test_scoping_seam_is_actually_used(self):
        """⭐ 数据权限卡点（spec §6.1）：本端点也必须过 `scope_queryset`。

        桩打在**返回值**上（清空），不是「被调用」上——视图完全可以调用它却把返回值丢掉
        （单独一行是合法 Python）。所以两半都要断：① `seam.called`（卡点真的被经过）；
        ② 候选真的变空（返回值真的被用上）。再配一条**不接桩的对照**，排除「空集来自别的条件」。

        ⚠️ 桩打在**消费方模块** `apps.business.api.settlement_views.scope_queryset`，
        ⛔ **不是**定义处 `apps.core.scoping.scope_queryset`——打在定义处不生效（E-0029）。
        照 `test_my_orders_api.py::test_scoping_seam_is_actually_used` 的既有先例。
        """
        self._perf(self._order(), user=self.taker_a)

        # 对照：不接桩时本来看得见（否则下面的空集可能来自别的原因）
        self.assertEqual([p["username"] for p in self._people()], ["taker-a"])

        with mock.patch(
            "apps.business.api.settlement_views.scope_queryset",
            # ⚠️ 桩的**签名**与真实调用一致：本线（按游戏权限收窄）起调用多传了
            #    `scope_filter=`（`scope_queryset(user, qs, scope_filter=game_scope(...))`）
            #    ⇒ 不收 `**kwargs` 会以 `TypeError` 红。**返回值与两条断言一字未改**
                        #    （同 `test_p3a_isolation.py` 那次处置；不是放宽判据）。
            side_effect=lambda user, qs, **kwargs: qs.none(),
        ) as seam:
            self.assertEqual(self._people(), [])

        self.assertTrue(seam.called, "人员候选没有经过 scope_queryset 卡点")


class AdjustmentChainTests(_P4ReadFixture):
    """`GET /api/business/performances/{id}/adjustments`：只返回**该绩效**的记录链。"""

    def test_only_the_rows_of_that_performance_are_returned(self):
        """哨兵：另一条绩效上的调整记录**不可见**。

        先证明哨兵真的在库里（在否定断言之前），否则「没返回它」可能只是因为库里本来没有。
        """
        mine = self._perf(self._order(), self.taker_a)
        other = self._perf(self._order(), self.taker_b)
        first = self._adj(mine, "-3.00")
        second = self._adj(mine, "7.50")
        sentinel = self._adj(other, "-99.00")
        self.assertIn(
            sentinel.id,
            list(PerformanceAdjustment.objects.values_list("id", flat=True)),
        )

        rows = self._items(adjustments_url(mine.id))

        self.assertEqual(sorted(r["id"] for r in rows), sorted([first.id, second.id]))
        self.assertNotIn(sentinel.id, [r["id"] for r in rows])
        self.assertEqual(
            sorted(Decimal(r["amount"]) for r in rows),
            sorted([Decimal("-3.00"), Decimal("7.50")]),
        )

    def test_the_chain_carries_settle_status_and_the_performance_id(self):
        perf = self._perf(self._order(), self.taker_a)
        self._adj(perf, "-3.00")
        self._adj(perf, "-4.00", settle=SettleStatus.SETTLED)

        rows = self._items(adjustments_url(perf.id))

        self.assertEqual(
            sorted(r["settle_status"] for r in rows), ["已结账", "未结账"]
        )
        self.assertEqual({r["performance_id"] for r in rows}, {perf.id})

    def test_the_chain_has_the_p2_pagination_shape(self):
        perf = self._perf(self._order(), self.taker_a)
        self._adj(perf, "-3.00")

        body = self._get(adjustments_url(perf.id)).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(set(body["data"]), {"items", "total", "page", "page_size"})
        self.assertEqual(body["data"]["total"], 1)

    def test_an_unknown_performance_is_an_empty_page_not_a_404(self):
        """路径里的 `performance_id` 是**筛选键**，不另做存在性检查：
        查不到就是空链（与「这条绩效没有任何调整记录」同一形状）。

        ⚠️ 这是**实施期拟定**的口径（spec §7 没写「绩效不存在」的处置）。
        选它的理由：链的语义是「筛选记录」，编造一个 404 会让前端对
        「不存在的 id」和「没有调整记录」写两套分支，而两者的处置完全一样。
        """
        body = self._get(adjustments_url(999999)).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["items"], [])
        self.assertEqual(body["data"]["total"], 0)

    def test_malformed_query_params_are_ignored_not_500(self):
        """本路由带路径转换器 ⇒ `test_no_route_500` 的显式路由腿**跳过它**（那是假红防护）。

        所以这里自己钉一次**本出口**：它**不读**筛选参数（只有分页器读 `page`），
        非法筛选值被忽略（`code 0`），只有 `?page=abc` 走分页器的 `1002` 分支。
        """
        perf = self._perf(self._order(), self.taker_a)
        self._adj(perf, "-3.00")

        ignored = self._get(
            adjustments_url(perf.id), {"emp_type": "BOGUS", "user": "abc"}
        )
        self.assertEqual(ignored.status_code, 200)
        self.assertEqual(ignored.json()["code"], 0)

        bad_page = self._get(adjustments_url(perf.id), {"page": "abc"})
        self.assertEqual(bad_page.status_code, 200)
        self.assertEqual(bad_page.json()["code"], 1002)


class ScopingWiringTests(_P4ReadFixture):
    """⭐ brief 第 7 条：三个查询的 `scope_queryset` 接线**真的被用上**（P3a 的教训）。

    ⚠️ **桩必须打在「返回值」上，不能只打在「被调用」上**：视图完全可以调用
    `scope_queryset(...)` 却把返回值丢掉（单独一行是合法 Python），所以「断言它被调用」
    会漏掉那种写法。这里的桩把结果**清空**，再断言端点真的返回空集；
    同时用**不接桩的对照**证明「那些行本来是看得见的」——排除「空集来自别的条件」。
    """

    def setUp(self):
        super().setUp()
        self.perf = self._perf(self._order(), self.taker_a)
        self.adjustment = self._adj(self.perf, "-3.00")
        # 「待归属」要的是「COMPLETED 且没有销售提成行」⇒ 上面那条单正好在清单里。
        self.visible_order_no = self.perf.order.order_no

    def test_the_settlement_list_uses_the_scoping_result(self):
        self.assertEqual(
            [r["id"] for r in self._items(SETTLEMENTS_URL)], [self.perf.id]
        )  # 对照：本来点得见

        with mock.patch(
            "apps.business.api.settlement_views.scope_queryset",
            side_effect=lambda user, qs, **kwargs: qs.none(),
        ) as seam:
            self.assertEqual(self._items(SETTLEMENTS_URL), [])

        self.assertTrue(seam.called, "结算列表没有经过 scope_queryset 卡点")

    def test_the_unattributed_list_uses_the_scoping_result(self):
        self.assertEqual(
            [r["order_no"] for r in self._items(UNATTRIBUTED_URL)],
            [self.visible_order_no],
        )  # 对照

        with mock.patch(
            "apps.business.api.settlement_views.scope_queryset",
            side_effect=lambda user, qs, **kwargs: qs.none(),
        ) as seam:
            self.assertEqual(self._items(UNATTRIBUTED_URL), [])

        self.assertTrue(seam.called, "待归属清单没有经过 scope_queryset 卡点")

    def test_the_adjustment_chain_uses_the_scoping_result(self):
        self.assertEqual(
            [r["id"] for r in self._items(adjustments_url(self.perf.id))],
            [self.adjustment.id],
        )  # 对照

        with mock.patch(
            "apps.business.api.adjustment_views.scope_queryset",
            side_effect=lambda user, qs, **kwargs: qs.none(),
        ) as seam:
            self.assertEqual(self._items(adjustments_url(self.perf.id)), [])

        self.assertTrue(seam.called, "调整记录链没有经过 scope_queryset 卡点")


class SettlementReadPermissionTests(TenantTestCase):
    """§9 #3：四个读接口（结算列表 / 待归属 / 调整记录链 / 结算对象人员）的闸门都是
    `has_node("finance.payroll")`，用**非管理员**双向钉住。

    ⚠️ **必须用非管理员账号**：`has_node_now` 对 `is_superuser` 有**短路**，用管理员构造会恒放行。
    每个接口两个方向：持有 → 200；不持有 → **403 且 `msg` 是权限语义**（P5 的 403 有多个来源，
    不钉 msg 的话「CSRF 失败」也会被当成「无权限」通过）。另配一条「持有**别的**节点仍 403」。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260920000000000001",
            status=OrderStatus.COMPLETED,
            hall_price=Decimal("100.00"),
        )
        self.taker = User.objects.create_user(username="taker-perm")
        self.perf = Performance.objects.create(
            order=self.order,
            user=self.taker,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("20.00"),
            bonus_amount=Decimal("20.00"),
            review_status=ReviewStatus.APPROVED,
            settle_status=SettleStatus.UNSETTLED,
        )
        PerformanceAdjustment.objects.create(
            performance=self.perf,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal("-3.00"),
            reason="权限用例",
            settle_status=SettleStatus.UNSETTLED,
        )

    def _grant(self, username, *codes):
        nodes = [
            PermissionNode.objects.get_or_create(
                code=code,
                defaults={
                    "name": code,
                    "note": f"测试节点 {code}",
                    "kind": NodeKind.API,
                    "level": 1,
                },
            )[0]
            for code in codes
        ]
        role = Role.objects.create(
            name=f"角{username}"[:5], group=Group.objects.create(name=f"g-{username}"[:150])
        )
        role.permissions.set(nodes)
        User.objects.get(username=username).groups.add(role.group)

    def test_a_holder_can_read_all_four_endpoints(self):
        client = make_logged_in_client(username="finance-with-node")
        self._grant("finance-with-node", NODE_CODE)

        for url in (
            SETTLEMENTS_URL,
            UNATTRIBUTED_URL,
            adjustments_url(self.perf.id),
            PEOPLE_URL,
        ):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["code"], 0)

    def test_a_user_without_the_node_is_denied_with_a_permission_message(self):
        client = make_logged_in_client(username="finance-without-node")

        for url in (
            SETTLEMENTS_URL,
            UNATTRIBUTED_URL,
            adjustments_url(self.perf.id),
            PEOPLE_URL,
        ):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 403)
                self.assertIn("权限", response.json()["msg"])

    def test_a_user_holding_a_different_node_is_still_denied(self):
        """持 `order.list`（另一个真节点）仍 403——否则「有任意权限就放行」也能绿。"""
        client = make_logged_in_client(username="finance-wrong-node")
        self._grant("finance-wrong-node", "order.list")

        response = client.get(SETTLEMENTS_URL)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["msg"], "你没有查看结算列表的权限")

    def test_the_four_endpoints_speak_their_own_permission_message(self):
        """四个 403 的 `msg` 各自可辨（照护「403 有多个来源」那条纪律）。

        ⚠️ `/people` 的期望值是**端点专属**那句（M-1）——⛔ 不要退回混入兜底的
        「你没有查看结算数据的权限」：那句是 `FinanceReadPermissionMixin` 的公共兜底，
        单独看分不出是哪条读路径给的 403。
        """
        client = make_logged_in_client(username="finance-no-node-2")

        messages = {}
        for url in (
            SETTLEMENTS_URL,
            UNATTRIBUTED_URL,
            adjustments_url(self.perf.id),
            PEOPLE_URL,
        ):
            response = client.get(url)
            self.assertEqual(response.status_code, 403)
            messages[url] = response.json()["msg"]

        self.assertEqual(
            messages[SETTLEMENTS_URL], "你没有查看结算列表的权限"
        )
        self.assertEqual(
            messages[UNATTRIBUTED_URL], "你没有查看待归属清单的权限"
        )
        self.assertEqual(
            messages[adjustments_url(self.perf.id)], "你没有查看绩效调整记录的权限"
        )
        self.assertEqual(
            messages[PEOPLE_URL], "你没有查看结算对象人员的权限"
        )

    def test_anonymous_is_401_not_403(self):
        """未认证 ⇒ **401**（spec §7 首段的分层：401 与 403 不能混）。

        这条守的是 `permission_denied` 的**转调写法**：它要给 403 换一句中文 `msg`，
        而实现必须转调 `super()`——DRF 在 `permission_denied` 里先判「有没有认证成功」，
        自己直接 `raise PermissionDenied` 会把匿名请求也变成 403。
        """
        anonymous = Client(SERVER_NAME="tenant.test.com")

        for url in (SETTLEMENTS_URL, UNATTRIBUTED_URL, adjustments_url(self.perf.id)):
            with self.subTest(url=url):
                self.assertEqual(anonymous.get(url).status_code, 401)


class SettlementOrderNoDualSearchTests(_P4ReadFixture):
    """⭐ D-0301（2026-10-24）：结算列表「订单号」精确框**双列 OR**——
    来源单号能精确命中 + 系统内置号照旧命中（仍是精确匹配，无模糊语义）。"""

    SRC = "SRC-TEST-SETTLE"

    def test_source_order_no_exact_match_hits(self):
        order = self._order()
        self._perf(order, self.taker_a)
        Order.objects.filter(pk=order.pk).update(source_order_no=self.SRC)
        rows = self._items(SETTLEMENTS_URL, {"order_no": self.SRC})
        self.assertEqual([row["order_no"] for row in rows], [order.order_no])

    def test_system_order_no_still_hits(self):
        order = self._order()
        self._perf(order, self.taker_a)
        Order.objects.filter(pk=order.pk).update(source_order_no=self.SRC)
        rows = self._items(SETTLEMENTS_URL, {"order_no": order.order_no})
        self.assertEqual([row["order_no"] for row in rows], [order.order_no])
        # ⚠️ 否定面：一个不存在的内置号不因来源号而误命中（仍是精确、不是 icontains）。
        # （夹具的单号是 `2026092000000{seq:07d}` ⇒ 这个带 9 段的值**不可能**是夹具单。）
        rows = self._items(SETTLEMENTS_URL, {"order_no": "20260920999999999999"})
        self.assertEqual(rows, [])
