"""「我的订单收入」页的三件后端能力（D-0365，2026-11-02）。

- `POST /api/business/my-orders/settle-requests/`：批量申请提前结算（逐条结果 + 当场重算额度）；
- `GET  /api/business/my-orders/export`：收入记录 xlsx 导出（筛选与列表同源 + 超上限信封）；
- `GET  /api/business/my-orders/?date_type=…&date_from=…&date_to=…`：日期四档筛选。

口径出处：实施计划 `docs/plans/2026-11-02-furlsta-filter-and-income-functions.md` §1 的八条
（已登记为 D-0365）；「提前结算申请」的判据链本体在 `apps/accounts/app_income_views
.create_settle_request`（App 端 2026-10-23 落地），本模块只测**网页端这层**的批量形状、
导出形状与日期映射——判据链内部（锁人 / 唯一约束 / 额度语义）由
`tests/test_app_income_api.py` 钉住，⛔ 不在那边之外重复第二遍。

夹具纪律（AGENTS.md §5-C/§5-G）：全部**合成值**（单号 = `20261102` 前缀 + 序列、
用户名 / 游戏名都是造的）；副作用断言打**数据库行**；跨人否定断言用
「租户内唯一且两方取值不同」的字段（这里用 `order_no`——每行必不同）。
"""
import datetime
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    PerformanceSettleRequest,
    SettleStatus,
)
from tests.p5_auth_helpers import make_logged_in_client
from tests.xlsx_helpers import rows_of, workbook_of

#: 两个新端点（形状见 `apps/business/urls.py`：批量带尾斜杠、导出没有）。
SETTLE_REQUESTS_URL = "/api/business/my-orders/settle-requests/"
EXPORT_URL = "/api/business/my-orders/export"
LIST_URL = "/api/business/my-orders/"

#: `/my-orders` 族的类级节点门（D-0128 冻结下的复用点）。
MY_ORDERS_NODES = ("my_orders.send_to_my_system",)

XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def _no(seq):
    """20 位合成单号（形状与生产同、值是本线造的）。"""
    return f"20261102{seq:012d}"


class IncomeFunctionsFixture(TenantTestCase):
    """一份能走到三个端点深处的最小数据：一个打手 + 可选的第二个打手。"""

    def setUp(self):
        self.game = Game.objects.create(name="收入功能线游戏")
        self.game2 = Game.objects.create(name="收入功能线副游戏")
        self.client = make_logged_in_client(
            username="income_fn_taker",
            groups=("代练员",),
            nodes=MY_ORDERS_NODES,
        )
        self.taker = User.objects.get(username="income_fn_taker")
        self.other = User.objects.create_user(username="income_fn_other")
        self._seq = 0

    # ── 夹具 ──────────────────────────────────────────────────────
    def _order(self, *, status=OrderStatus.COMPLETED, hall_price="100.00", game=None,
               seq=None):
        self._seq += 1
        return Order.objects.create(
            game=game or self.game,
            order_no=_no(seq if seq is not None else self._seq),
            status=status,
            total_price=Decimal("300.00"),
            hall_price=None if hall_price is None else Decimal(hall_price),
        )

    def _perf(
        self,
        order,
        *,
        user=None,
        emp_type=EmpType.MAIN,
        temp="50.00",
        bonus="50.00",
        taker_price=None,
        settle_status=SettleStatus.UNSETTLED,
        claimed_at=None,
        settled_at=None,
    ):
        return Performance.objects.create(
            order=order,
            emp_type=emp_type,
            user=self.taker if user is None else user,
            temp_amount=Decimal(temp),
            bonus_amount=Decimal(bonus),
            taker_price=None if taker_price is None else Decimal(taker_price),
            settle_status=settle_status,
            settled_at=settled_at,
            claimed_at=claimed_at,
        )

    def _post_json(self, url, payload):
        return self.client.post(
            url, data=json.dumps(payload), content_type="application/json"
        )


# ── B1：批量申请提前结算 ──────────────────────────────────────────────


class SettleRequestsBatchTests(IncomeFunctionsFixture):
    """`POST /my-orders/settle-requests/`：逐条结果 + 额度当场重算。"""

    def _apply(self, ids):
        response = self._post_json(SETTLE_REQUESTS_URL, {"performance_ids": ids})
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def test_a_single_success_returns_payload_and_recomputes_quota(self):
        perf = self._perf(self._order())

        data = self._apply([perf.id])

        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["performance_id"], perf.id)
        self.assertTrue(item["ok"])
        self.assertEqual(item["code"], 0)
        self.assertEqual(item["settle_request"]["performance_id"], perf.id)
        self.assertEqual(item["settle_request"]["state"], "pending")
        # 副作用打数据库行（不是打响应就完事）。
        row = PerformanceSettleRequest.objects.get(performance=perf)
        self.assertEqual(row.user, self.taker)
        self.assertEqual(row.state, PerformanceSettleRequest.State.PENDING)
        # 额度**当场重算**：申请已落库 ⇒ used 已含这一条。
        self.assertEqual(data["early_settlement"], {"used": 1, "limit": 3})

    def test_a_mixed_batch_reports_each_item_and_keeps_going(self):
        """混批：合法 / 别人的 / 未完成单——逐条各回各的，坏条**不中断整批**。"""
        ok_perf = self._perf(self._order())
        foreign = self._perf(self._order(), user=self.other)          # 别人的
        not_completed = self._perf(self._order(status=OrderStatus.IN_PROGRESS))

        data = self._apply([ok_perf.id, foreign.id, not_completed.id])

        self.assertEqual([item["ok"] for item in data["items"]], [True, False, False])
        # 别人的 / 不存在的 ⇒ 同一句合并文案（不给越权请求一个探测面）。
        self.assertEqual(data["items"][1]["msg"], "该绩效不存在或不属于你")
        self.assertEqual(data["items"][1]["code"], 1035)
        # 未完成单 ⇒ 1035 的订单未完成文案（1035/1036 文案原样透传，前端不翻译）。
        self.assertIn("订单未完成", data["items"][2]["msg"])
        self.assertEqual(data["items"][2]["code"], 1035)
        # 只有合法那条落了库。
        self.assertEqual(PerformanceSettleRequest.objects.count(), 1)
        self.assertEqual(data["early_settlement"]["used"], 1)

    @override_settings(EARLY_SETTLE_LIMIT=2)
    def test_quota_exhaustion_reports_limit_code_per_item_from_the_third_on(self):
        """额度 2：第 1、2 条成功，同批的第 3 条起逐条 1036；额度只被真成功的占。"""
        perfs = [self._perf(self._order()) for _ in range(3)]

        data = self._apply([p.id for p in perfs])

        self.assertEqual([item["ok"] for item in data["items"]], [True, True, False])
        self.assertEqual(data["items"][2]["code"], 1036)
        self.assertIn("额度已用完", data["items"][2]["msg"])
        self.assertEqual(data["early_settlement"], {"used": 2, "limit": 2})

    def test_settled_performance_is_rejected_with_the_settled_message(self):
        perf = self._perf(self._order(), settle_status=SettleStatus.SETTLED)

        data = self._apply([perf.id])

        self.assertFalse(data["items"][0]["ok"])
        self.assertIn("已结账", data["items"][0]["msg"])
        self.assertEqual(data["items"][0]["code"], 1035)
        self.assertEqual(PerformanceSettleRequest.objects.count(), 0)

    def test_an_already_requested_performance_is_rejected_with_the_same_message(self):
        perf = self._perf(self._order())
        PerformanceSettleRequest.objects.create(performance=perf, user=self.taker)

        data = self._apply([perf.id])

        self.assertFalse(data["items"][0]["ok"])
        self.assertIn("已申请过", data["items"][0]["msg"])
        # 库里仍是那一条，没有被复制成两条。
        self.assertEqual(PerformanceSettleRequest.objects.count(), 1)

    def test_duplicate_ids_are_not_special_cased_and_reported_per_item(self):
        """重复 id 不特判：第一条成功、第二条撞「已申请过」，逐条如实呈现。"""
        perf = self._perf(self._order())

        data = self._apply([perf.id, perf.id])

        self.assertEqual([item["ok"] for item in data["items"]], [True, False])
        self.assertIn("已申请过", data["items"][1]["msg"])

    def test_malformed_payloads_are_business_codes_not_500(self):
        """空列表 / 非 int 元素 / 超上限 200 ⇒ 都是 `1000`（不 500、不截断）。"""
        cases = [
            ("空列表", {"performance_ids": []}),
            ("元素不是整数", {"performance_ids": ["x"]}),
            ("元素是 null", {"performance_ids": [None]}),
            ("不是列表", {"performance_ids": 3}),
            ("超上限", {"performance_ids": list(range(1, 202))}),
            ("缺键", {}),
        ]
        for label, payload in cases:
            with self.subTest(case=label):
                response = self._post_json(SETTLE_REQUESTS_URL, payload)
                self.assertEqual(response.status_code, 200, response.content[:200])
                self.assertEqual(response.json()["code"], 1000, response.json())
        # 上面任何一条都**没有**写出申请行。
        self.assertEqual(PerformanceSettleRequest.objects.count(), 0)


# ── B2：收入导出 ──────────────────────────────────────────────────────


class MyOrdersExportTests(IncomeFunctionsFixture):
    """`GET /my-orders/export`：xlsx 形状 / 筛选同源 / 数据范围 / 超上限 / 留痕。"""

    def _export(self, query=None):
        return self.client.get(EXPORT_URL, query or {})

    def test_success_returns_an_xlsx_with_the_income_sheet(self):
        perf = self._perf(
            self._order(hall_price="80.00", seq=901),
            temp="66.00",
            bonus="66.00",
            taker_price="80.00",
        )
        # 来源单号：`blank=True` 的非 NULL 文本列，夹具显式给一个合成值再断言它真的出格。
        Order.objects.filter(pk=perf.order.pk).update(
            source_order_no="TB-合成-收入导出来源"
        )
        PerformanceSettleRequest.objects.create(performance=perf, user=self.taker)

        response = self._export()

        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertTrue(
            response["Content-Type"].startswith(XLSX_MIME), response["Content-Type"]
        )
        workbook = workbook_of(response)
        rows = rows_of(workbook, "收入记录")
        # 表头 13 列 + 一条数据行。
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "用户名称")
        self.assertEqual(len(rows[0]), 13)
        self.assertEqual(rows[1][0], "income_fn_taker")
        # 订单状态出**中文**（后端词表的 label），来源单号 / 申请时刻都是真值列。
        self.assertEqual(rows[1][9], "已完成")
        self.assertEqual(rows[1][5], "TB-合成-收入导出来源")
        self.assertNotEqual(rows[1][12], "")

    def test_export_is_scoped_to_the_requesting_user_only(self):
        """跨人否定断言：A 导出看不到 B 的行（判据 = 租户内唯一、两方取值不同的 `order_no`）。"""
        mine = self._perf(self._order(seq=901), user=self.taker)
        theirs = self._perf(self._order(seq=902), user=self.other)

        response = self._export()

        rows = rows_of(workbook_of(response), "收入记录")[1:]
        order_nos = [row[4] for row in rows]
        self.assertIn(mine.order.order_no, order_nos)
        # 跨人否定：对方那行的单号**不在**文件里（不是「行数少一」这种可被巧合凑出的判据）。
        self.assertNotIn(theirs.order.order_no, order_nos)

    def test_sales_commission_rows_are_not_my_orders(self):
        """`销售提成` 行不是「我接的单」⇒ 不进导出（与列表同一段基线查询集）。"""
        self._perf(self._order(), emp_type=EmpType.SALES, bonus="999.00")

        response = self._export()

        self.assertEqual(len(rows_of(workbook_of(response), "收入记录")), 1)  # 只有表头

    def test_mirrored_filters_narrow_the_rows_like_the_page(self):
        """筛选同源：game 名精确 / settle_status 中文 / order_status 逗串 / key1·key2 icontains。"""
        hit = self._perf(self._order(game=self.game, seq=911), bonus="11.00")
        miss_game = self._perf(self._order(game=self.game2, seq=912), bonus="12.00")
        miss_settle = self._perf(
            self._order(seq=913), settle_status=SettleStatus.SETTLED, bonus="13.00"
        )
        miss_status = self._perf(
            self._order(seq=914, status=OrderStatus.IN_PROGRESS), bonus="14.00"
        )
        miss_key1 = self._perf(self._order(seq=915), bonus="15.00")
        miss_key2 = self._perf(self._order(seq=916), bonus="16.00")

        cases = [
            ("game 名精确", {"game": self.game.name}, hit, miss_game),
            ("settle_status 中文", {"settle_status": "未结账"}, hit, miss_settle),
            ("order_status 逗串", {"order_status": "COMPLETED,ISSUE"}, hit, miss_status),
            # ⚠️ key1 的子串取单号**尾 4 位**（判别窗口）：取中段会把所有行都命中
            # （合成单号共享前缀），阳性/阴性对照都会失真。
            ("key1 单号子串", {"key1": hit.order.order_no[-4:]}, hit, miss_key1),
        ]
        for label, query, expected, missing in cases:
            with self.subTest(filter=label):
                response = self._export(query)
                order_nos = [
                    row[4] for row in rows_of(workbook_of(response), "收入记录")[1:]
                ]
                self.assertIn(expected.order.order_no, order_nos)
                self.assertNotIn(missing.order.order_no, order_nos)
        # key1/key2 的阴性对照明确钉住：不匹配的行不出现。
        Order.objects.filter(pk=hit.order.pk).update(project_text="深渊满星十二层")
        for query, expected, missing in (
            ({"key1": "999999"}, None, miss_key1),
            ({"key2": "深渊"}, hit, miss_key2),
            ({"key2": "不存在"}, None, miss_key2),
        ):
            with self.subTest(filter="key icontains", query=query):
                response = self._export(query)
                order_nos = [
                    row[4] for row in rows_of(workbook_of(response), "收入记录")[1:]
                ]
                if expected is not None:
                    self.assertIn(expected.order.order_no, order_nos)
                self.assertNotIn(missing.order.order_no, order_nos)
        # 阳性对照（防「整个筛选把表筛空」假绿）：不带任何筛选 ⇒ 六行全出。
        response = self._export()
        order_nos = [row[4] for row in rows_of(workbook_of(response), "收入记录")[1:]]
        self.assertEqual(len(order_nos), 6)

    def test_malformed_query_params_return_the_envelope_not_a_file(self):
        """畸形查询参数 ⇒ 200 + 信封（explicit-routes 腿的形状要求，⛔ 不是 500 / 半截文件）。"""
        cases = [
            {"settle_status": "BOGUS"},
            {"order_status": "IN_PROGRESS,BOGUS"},
            {"order_status": ",,"},
            {"date_type": "BOGUS"},
            {"date_from": "garbage"},
            {"game": "x" * 100},
        ]
        for query in cases:
            with self.subTest(query=query):
                response = self._export(query)
                self.assertEqual(response.status_code, 200, response.content[:200])
                self.assertEqual(response.json()["code"], 1000, response.json())
                self.assertTrue(response["Content-Type"].startswith("application/json"))

    def test_over_the_row_cap_returns_the_envelope_not_a_partial_file(self):
        """超上限**报错不截断**：上限与绩效导出共用一份常量，这里压到 1 量真实形状。"""
        self._perf(self._order(seq=921), user=self.taker)
        self._perf(self._order(seq=922), user=self.taker)

        with mock.patch("apps.business.api.my_orders_views.EXPORT_MAX_ROWS", 1):
            response = self._export()

        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 1000, response.json())
        msg = response.json()["msg"]
        self.assertIn("本次导出 2 行", msg)
        self.assertIn("超过上限 1 行", msg)
        self.assertNotEqual(response["Content-Type"], XLSX_MIME)

    def test_only_successful_exports_are_logged(self):
        """op-log 照 `PerformanceExportView` 先例：成功记一条；畸形参数（没导出东西）不记。"""
        self._perf(self._order(seq=931), user=self.taker)

        response = self._export()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            OperationLog.objects.filter(action="my_orders.export").count(), 1
        )
        # 留痕只承载「谁导了」：不写 target_kind / target_id（照先例）。
        log = OperationLog.objects.get(action="my_orders.export")
        self.assertEqual(log.target_kind, "")
        self.assertEqual(log.target_id, "")

        self._export({"settle_status": "BOGUS"})     # 畸形 ⇒ 提前 return，没导出
        self.assertEqual(
            OperationLog.objects.filter(action="my_orders.export").count(), 1
        )


# ── B3：日期四档 ──────────────────────────────────────────────────────


class MyOrdersDateFilterTests(IncomeFunctionsFixture):
    """`GET /my-orders/?date_type=…`：四档各自命中 + 排除；`claimed` 空值行被排除必须有。"""

    #: 本地时区下的「某一天」。四列各打这一天、错开列用前一天 ⇒ 命中 / 排除都判得出。
    DAY = "2026-11-01"
    OTHER_DAY = "2026-10-31"

    def setUp(self):
        super().setUp()
        # ⚠️ `Performance.created_at` 与 `Order.created_at` 都是 `auto_now_add` ⇒
        #    `create()` 里传不进去；统一在落库后用 `update()` 写（`update()` 不触发
        #    auto_now_add）。命中行：四列都在 DAY；排除行：四列都在 OTHER_DAY
        #    （哪个档筛 DAY 都该排掉它）。时间值用 make_aware（timezone 环境下
        #    naive datetime 会触发 RuntimeWarning，也让「本地日期切天」的判据更实）。
        def at(day, hour):
            return timezone.make_aware(
                datetime.datetime.strptime(f"{day} {hour:02d}:00:00", "%Y-%m-%d %H:%M:%S")
            )

        self.hit = self._perf(self._order(seq=941))
        self.miss = self._perf(self._order(seq=942))
        Performance.objects.filter(pk=self.hit.pk).update(
            claimed_at=at(self.DAY, 10),
            created_at=at(self.DAY, 11),
            settled_at=at(self.DAY, 13),
        )
        Performance.objects.filter(pk=self.miss.pk).update(
            claimed_at=at(self.OTHER_DAY, 10),
            created_at=at(self.OTHER_DAY, 11),
            settled_at=at(self.OTHER_DAY, 13),
        )
        Order.objects.filter(pk=self.hit.order.pk).update(
            submitted_at=at(self.DAY, 12),
            created_at=at(self.DAY, 11),
        )
        Order.objects.filter(pk=self.miss.order.pk).update(
            submitted_at=at(self.OTHER_DAY, 12),
            created_at=at(self.OTHER_DAY, 11),
        )

    def _list_order_nos(self, query):
        response = self.client.get(LIST_URL, query)
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return [item["order_no"] for item in body["data"]["items"]]

    def test_each_date_type_selects_its_own_column_for_the_same_window(self):
        for date_type in ("claimed", "submitted", "created", "settled"):
            with self.subTest(date_type=date_type):
                order_nos = self._list_order_nos(
                    {"date_type": date_type, "date_from": self.DAY, "date_to": self.DAY}
                )
                self.assertIn(self.hit.order.order_no, order_nos)
                self.assertNotIn(self.miss.order.order_no, order_nos)

    def test_claimed_rows_with_a_null_column_are_excluded(self):
        """`claimed_at` 可空 ⇒ 按那一档筛时空值行**自然被排除**（先例同款口径，必须有）。"""
        null_row = self._perf(self._order(seq=943), claimed_at=None)
        self.assertIsNone(null_row.claimed_at)

        order_nos = self._list_order_nos(
            {"date_type": "claimed", "date_from": self.DAY, "date_to": self.DAY}
        )

        self.assertNotIn(null_row.order.order_no, order_nos)
        self.assertIn(self.hit.order.order_no, order_nos)

    def test_dates_without_a_date_type_are_ignored(self):
        """`date_type` 缺省 ⇒ 日期不生效（不猜默认列）：两行都回来。"""
        order_nos = self._list_order_nos(
            {"date_from": self.DAY, "date_to": self.DAY}
        )
        self.assertIn(self.hit.order.order_no, order_nos)
        self.assertIn(self.miss.order.order_no, order_nos)

    def test_malformed_date_params_are_business_codes(self):
        """非法档 / 非法日期 ⇒ `1000`（E-0010：请求来源的值先进序列化器）。"""
        for query in (
            {"date_type": "BOGUS", "date_from": self.DAY},
            {"date_type": "claimed", "date_from": "garbage"},
            {"date_type": "claimed", "date_to": "garbage"},
        ):
            with self.subTest(query=query):
                response = self.client.get(LIST_URL, query)
                self.assertEqual(response.status_code, 200, response.content[:200])
                self.assertEqual(response.json()["code"], 1000, response.json())

    def test_empty_date_values_are_treated_as_not_sent(self):
        """空串按「没传」（前端常送 `?date_from=`）⇒ code 0、筛选不生效。"""
        order_nos = self._list_order_nos(
            {"date_type": "claimed", "date_from": "", "date_to": ""}
        )
        self.assertIn(self.hit.order.order_no, order_nos)
        self.assertIn(self.miss.order.order_no, order_nos)
