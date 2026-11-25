"""订单导出 `.xlsx` 的构造（spec §3 / §4 / §5；实施计划 Task 1）。

⚠️ 期望值一律写**字面值**（E-0024）：31 个表头逐字抄进本文件，
⛔ 不引被测模块的 `ORDER_EXPORT_HEADERS`——自指断言在被测常量被改坏时不会红。
"""
import re
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from django.contrib.auth.models import User
from django.test import SimpleTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from apps.business import my_orders_export, order_export, performance_export
from apps.business.models import (
    Game, GameServer, Order, OrderStatus, Performance, ProjectCategory,
)
from tests.p5_auth_helpers import make_logged_in_client
from tests.xlsx_helpers import workbook_of

#: 31 个表头（**字面副本**，逐字来自 spec §3 的表格）。
EXPECTED_HEADERS = [
    "订单号", "系统内置单号", "来源", "游戏名称", "服务器",
    "格式化订单内容横排", "格式化订单内容", "自定义标记", "重要备注", "代练/陪玩项目",
    "录单人", "录单时间", "订单状态", "角色名", "接单价",
    "实收款", "退款", "游戏账号", "接手时间", "完成时间",
    "退款时间", "总计发单价", "发单人", "子副账号", "当前接手人",
    "接单客服", "电话", "角色信息", "项目分类", "旺旺",
    "QQ/微信",
]
NUMERIC_INDEXES = (14, 15, 16, 21)
TIME_COLUMNS = (11, 18, 19, 20)
TIME_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
#: 工作表名的**字面副本**（⛔ 不引 `order_export.SHEET_TITLE`——自指断言在被测常量被改坏时不会红，E-0024）。
SHEET_TITLE_LITERAL = "Sheet1"

MOMENT = datetime(2026, 9, 27, 10, 20, 30, tzinfo=timezone.get_current_timezone())


class OrderExportWorkbookTests(TenantTestCase):
    def setUp(self):
        # ⚠️ 夹具**只建一次**（`User.username` 唯一、`Order.order_no` 唯一）⇒ 多调用一次 `_order()`
        # 不能再建同名用户。
        self.game = Game.objects.create(name="测试游戏甲")
        # `GameServer.game` / `ProjectCategory.game` 都是非空 FK ⇒ 建时必须带上所属游戏。
        self.server = GameServer.objects.create(game=self.game, name="测试区服甲")
        self.category = ProjectCategory.objects.create(game=self.game, name="测试分类甲")
        # 「发单人」列导出的是**显示名**（2026-11-05 业务方：不许出登录账号）⇒ 夹具带昵称，
        # 期望取昵称——旧实现（读 `username`）会让本网变红（昵称 ≠ 账号）。
        self.publisher = User.objects.create_user(
            username="测试发单人甲", first_name="发单昵称乙", password="x"
        )

    def _order(self, **overrides):
        base = dict(
            order_no="20260927000000000001",
            source_order_no="",
            source="测试来源甲",
            game=self.game,
            server=self.server,
            category=self.category,
            hall_publisher=self.publisher,
            status=OrderStatus.IN_PROGRESS,
            project_text="测试项目甲",
            content="第一行\r\n第二行\r第三行\n第四行",
            tag="测试标记",
            remark="测试备注",
            game_account="acct-甲",
            game_sub_account="sub-甲",
            game_role_name="测试角色甲",
            created_by="测试录单人甲",
            total_price=Decimal("123.00"),
            hall_price=Decimal("88.00"),
            refund_fee=Decimal("0.00"),
            sales_agent="测试客服甲",
            customer_phone="000-0000-0000",
            customer_ww="测试旺旺",
            customer_qq="测试Q",
            completed_at=MOMENT,
        )
        base.update(overrides)
        return Order.objects.create(**base)

    def _rows(self, order, *, claimed_at=MOMENT, refunded_at=None,
              taker_name="测试打手乙", can_view_offer_price=True, can_view_taker=True):
        """把 T3 会给的三处注解 / 请求级判据手工挂到实例上，再取 31 格。"""
        order.export_claimed_at = claimed_at
        order.export_refunded_at = refunded_at
        order.taker_name = taker_name
        return order_export.row_values(
            order,
            can_view_offer_price=can_view_offer_price,
            can_view_taker=can_view_taker,
        )

    # ── B1：列序与表头逐字 31/31；单 sheet `Sheet1` ─────────────────────────
    def test_headers_are_exactly_the_31_literal_strings(self):
        book = load_workbook(BytesIO(
            order_export.build_workbook([self._rows(self._order())])
        ))
        self.assertEqual(list(order_export.ORDER_EXPORT_HEADERS), EXPECTED_HEADERS)
        self.assertEqual(len(EXPECTED_HEADERS), 31)
        self.assertEqual(book.sheetnames, [SHEET_TITLE_LITERAL])
        sheet = book[SHEET_TITLE_LITERAL]
        self.assertEqual([cell.value for cell in sheet[1]], EXPECTED_HEADERS)
        self.assertEqual(sheet.max_column, 31)

    # ── B3：样式 / 行高 / number_format / 不冻结 / 不筛选 / 不合并 ──────────
    def test_shape_and_styles_match_the_reference(self):
        book = load_workbook(BytesIO(
            order_export.build_workbook([self._rows(self._order()), self._rows(self._order(
                order_no="20260927000000000002"))])
        ))
        sheet = book[SHEET_TITLE_LITERAL]
        self.assertEqual(sheet.row_dimensions[1].height, 32.0)
        self.assertEqual(sheet.row_dimensions[2].height, 25.0)
        self.assertEqual(sheet.row_dimensions[3].height, 25.0)
        for row in sheet.iter_rows():
            for cell in row:
                self.assertEqual(cell.alignment.horizontal, "center", cell.coordinate)
                self.assertEqual(cell.alignment.vertical, "center", cell.coordinate)
                self.assertEqual(cell.number_format, "General", cell.coordinate)
        for cell in sheet[1]:
            self.assertEqual(cell.fill.patternType, "solid", cell.coordinate)
            self.assertEqual(cell.fill.fgColor.rgb, "FF808080", cell.coordinate)
        self.assertIsNone(sheet.freeze_panes)
        self.assertIsNone(sheet.auto_filter.ref)
        self.assertEqual(list(sheet.merged_cells.ranges), [])

    # ── B2：列宽（数值列 13.0 / 其余 20.0）──────────────────────────────────
    def test_column_widths_follow_the_numeric_rule(self):
        book = load_workbook(BytesIO(order_export.build_workbook([])))
        sheet = book[SHEET_TITLE_LITERAL]
        for index in range(31):
            letter = get_column_letter(index + 1)
            expected = 13.0 if index in NUMERIC_INDEXES else 20.0
            self.assertEqual(sheet.column_dimensions[letter].width, expected, letter)

    # ── B4：四个时间列写**字符串** ─────────────────────────────────────────
    def test_time_columns_are_strings_at_second_precision(self):
        rows = [self._rows(self._order(), refunded_at=MOMENT)]
        sheet = load_workbook(BytesIO(order_export.build_workbook(rows)))[SHEET_TITLE_LITERAL]
        for index in TIME_COLUMNS:
            value = sheet.cell(row=2, column=index + 1).value
            self.assertIsInstance(value, str, index)
            self.assertRegex(value, TIME_SHAPE, index)

    # ── B5 / B5b：金额列是数字或空串 `''`；无退款写 `0` ──────────────────────
    def test_money_columns_are_numbers(self):
        rows = [self._rows(self._order())]
        sheet = load_workbook(BytesIO(order_export.build_workbook(rows)))[SHEET_TITLE_LITERAL]
        for index in NUMERIC_INDEXES:
            value = sheet.cell(row=2, column=index + 1).value
            self.assertIsInstance(value, (int, float, Decimal), index)
        self.assertEqual(Decimal(str(sheet.cell(row=2, column=17).value)), Decimal("0"))

    def test_hall_price_none_or_redacted_is_an_empty_string(self):
        # ⚠️ 同一行**建一次、两种判据各导一次**（计划里的两次 `_order(order_no=…)` 会在第二次
        # 撞 `order_no` 唯一约束 ⇒ 执行期修订）。
        unset = self._order(order_no="20260927000000000003", hall_price=None)
        for can_view_offer_price in (True, False):
            row = self._rows(unset, can_view_offer_price=can_view_offer_price)
            self.assertEqual(row[21], "")          # 空值 ⇒ 空串，⛔ 不是 0、不是 None
        priced = self._order(order_no="20260927000000000004")
        self.assertEqual(
            Decimal(str(self._rows(priced, can_view_offer_price=True)[21])),
            Decimal("88.00"),
        )
        self.assertEqual(
            self._rows(priced, can_view_offer_price=False)[21],
            "",                                    # 脱敏 ⇒ 空串（fail-closed）
        )

    # ── 第 6 列换行压缩（不折叠）／第 7 列原样 ─────────────────────────────
    def test_column_six_flattens_newlines_without_collapsing(self):
        row = self._rows(self._order(content="甲\n\n乙"))
        self.assertEqual(row[5], "甲  乙")         # `\n\n` ⇒ **两个**空格（⛔ 不折叠）
        row = self._rows(self._order(
            order_no="20260927000000000005", content="甲\r\n乙\r丙\n丁"))
        self.assertEqual(row[5], "甲 乙 丙 丁")

    def test_column_seven_keeps_the_original_newlines(self):
        row = self._rows(self._order(content="甲\n\n乙"))
        self.assertEqual(row[6], "甲\n\n乙")

    # ── 第 25 列：当前接手人（无 MAIN 行 / 无权查看 ⇒ 空串）────────────────
    def test_taker_column_is_blank_when_redacted_or_missing(self):
        # spec §3 第 25 行：无 MAIN 行 ⇒ 空串；`can_view_taker(user)` 为假 ⇒ 空串（fail-closed）。
        # ⛔ 不写「无权查看」这个哨兵——那是页面那一列的行为，导出件不泄露姓名。
        order = self._order()
        self.assertEqual(self._rows(order)[24], "测试打手乙")
        self.assertEqual(self._rows(order, can_view_taker=False)[24], "")
        self.assertEqual(self._rows(order, taker_name=None)[24], "")

    # ── spec §3 的 31 行口径**逐格钉值**（ §6-A 点名的 20 列缺口）──
    def test_all_31_cells_are_pinned_by_literal_values(self):
        """一行订单，31 个来源各取一个**互不相同**的合成值，再逐格 `assertEqual` 到字面量。

        ⚠️ 期望值全部是本文件里的**字面量**（E-0024）——⛔ 不引 `order_export` 的任何常量。
        ⚠️ **第 16 列「实收款」在此同时钉住两件事**（评审 §8-次要 2）：它等于**派生**的
        `net_received`（接单价 − 累计退款 = 321.00 − 21.00 = 300.00），**且⛔ 不是**模型上那个
        基本恒 0 的 `received_price`——该字段本用例**故意设成 `999.99`**（与派生值**取值不同**）
        ⇒ 实现若改读 `received_price`，本用例必红（不再只「红在类型上」）。
        """
        game = Game.objects.create(name="合成游戏丙")
        server = GameServer.objects.create(game=game, name="合成区服丁")
        category = ProjectCategory.objects.create(game=game, name="合成分类巳")
        created_moment = datetime(2026, 9, 27, 11, 22, 33, tzinfo=timezone.get_current_timezone())
        claimed_moment = datetime(2026, 9, 27, 12, 33, 44, tzinfo=timezone.get_current_timezone())
        completed_moment = datetime(2026, 9, 27, 13, 44, 55, tzinfo=timezone.get_current_timezone())
        refunded_moment = datetime(2026, 9, 27, 14, 55, 6, tzinfo=timezone.get_current_timezone())
        order = self._order(
            order_no="20260927000000000011",
            source_order_no="合成来源单号甲",
            source="合成来源乙",
            game=game,
            server=server,
            category=category,
            content="合成内容戊\r\n合成内容己",
            tag="合成标记庚",
            remark="合成备注辛",
            project_text="合成项目壬",
            created_by="合成录单人癸",
            status=OrderStatus.IN_PROGRESS,
            game_role_name="合成角色子",
            total_price=Decimal("321.00"),
            received_price=Decimal("999.99"),   # ⛔ 第 16 列**不许**读它（派生值要 300.00）
            refund_fee=Decimal("21.00"),
            game_account="合成账号丑",
            completed_at=completed_moment,
            hall_price=Decimal("456.00"),
            game_sub_account="合成子账号寅",
            sales_agent="合成客服辰",
            customer_phone="000-0000-0001",
            customer_ww="合成旺旺午",
            customer_qq="合成企鹅未",
        )
        # 第 12 列的来源是**真实字段** `created_at`（`auto_now_add`）⇒ 用 `update()` 落库后重读，
        # ⛔ 不在内存里伪造字段值。
        Order.objects.filter(pk=order.pk).update(created_at=created_moment)
        order.refresh_from_db()
        # 第 19 / 21 列走 T3 的 `Subquery` 注解、第 25 列走 `taker_name` 注解
        # ⇒ 本用例只钉「**注解 → 单元格文本**」这一步（取数语义本身在 T3 的用例里）。
        order.export_claimed_at = claimed_moment
        order.export_refunded_at = refunded_moment
        order.taker_name = "合成打手卯"
        row = order_export.row_values(order, can_view_offer_price=True, can_view_taker=True)
        self.assertEqual(len(row), 31)
        expected = [
            "合成来源单号甲",                     # 1 订单号（`source_order_no` 非空 ⇒ 写它）
            "20260927000000000011",               # 2 系统内置单号
            "合成来源乙",                         # 3 来源
            "合成游戏丙",                         # 4 游戏名称
            "合成区服丁",                         # 5 服务器
            "合成内容戊 合成内容己",               # 6 格式化订单内容横排（`\r\n` 换一个空格）
            "合成内容戊\r\n合成内容己",            # 7 格式化订单内容（原样）
            "合成标记庚",                         # 8 自定义标记
            "合成备注辛",                         # 9 重要备注
            "合成项目壬",                         # 10 代练/陪玩项目
            "合成录单人癸",                       # 11 录单人
            "2026-09-27 11:22:33",                # 12 录单时间
            "代练中",                             # 13 订单状态（中文 label，⛔ 不是 `IN_PROGRESS`）
            "合成角色子",                         # 14 角色名
            Decimal("321.00"),                    # 15 接单价
            Decimal("300.00"),                    # 16 实收款（派生；⛔ 不是 received_price 的 999.99）
            Decimal("21.00"),                     # 17 退款
            "合成账号丑",                         # 18 游戏账号
            "2026-09-27 12:33:44",                # 19 接手时间（注解）
            "2026-09-27 13:44:55",                # 20 完成时间（`completed_at`）
            "2026-09-27 14:55:06",                # 21 退款时间（注解）
            Decimal("456.00"),                    # 22 总计发单价
            "发单昵称乙",                         # 23 发单人（显示名：昵称优先，⛔ 不是登录账号）
            "合成子账号寅",                       # 24 子副账号
            "合成打手卯",                         # 25 当前接手人（注解）
            "合成客服辰",                         # 26 接单客服
            "000-0000-0001",                      # 27 电话
            "[合成游戏丙]合成区服丁",              # 28 角色信息（模板，⛔ 不是占位串）
            "合成分类巳",                         # 29 项目分类
            "合成旺旺午",                         # 30 旺旺
            "合成企鹅未",                         # 31 QQ/微信
        ]
        self.assertEqual(len(expected), 31)
        for index, value in enumerate(expected):
            self.assertEqual(row[index], value, f"第 {index + 1} 列")
        # 第 1 列的回落规则**两向都钉**：`source_order_no` 为空 ⇒ 写 `order_no`。
        fallback = self._order(order_no="20260927000000000012", source_order_no="")
        self.assertEqual(self._rows(fallback)[0], "20260927000000000012")

    def test_created_by_cell_is_mapped_through_the_names_dict(self):
        """第 11 列「录单人」= 映射 dict 给的显示名（2026-11-05 业务方：导出不出登录账号）。

        `created_by` 存的是**账号快照**（下游按账号消费，存储值不动）⇒ 显示名由视图批量
        映射传进来。上面 31 格主用例没传 `created_by_names` ⇒ 钉的就是**回落档**（原值）；
        本用例钉**映射档**——漏传 / 漏查映射 ⇒ 这里直接红（显示名 ≠ 账号）。
        """
        order = self._order(order_no="20260927000000000013", created_by="合成录单人癸")
        # 第 19 / 21 / 25 列走视图侧注解（本函数不碰库）⇒ 内存赋值照 31 格主用例的形状。
        order.export_claimed_at = None
        order.export_refunded_at = None
        order.taker_name = ""
        row = order_export.row_values(
            order,
            can_view_offer_price=True,
            can_view_taker=True,
            created_by_names={"合成录单人癸": "录单昵称子"},
        )
        self.assertEqual(row[10], "录单昵称子")
        # 映射里没有的账号回落原值（历史快照 / 已硬删）。
        order.export_claimed_at = None
        order.export_refunded_at = None
        order.taker_name = ""
        row_miss = order_export.row_values(
            order,
            can_view_offer_price=True,
            can_view_taker=True,
            created_by_names={"别的账号": "别的人"},
        )
        self.assertEqual(row_miss[10], "合成录单人癸")


class OrderExportSanitizeApiTests(TenantTestCase):
    """E-0249 回归钉：`remark` / `project_text` 含非法控制字符的合成单进导出。

    修前形状（E-0249 的现场）：`build_workbook` 把列值**原样** `sheet.append` ⇒ 一行脏字符
    （openpyxl 判定的 XML 非法控制字符，如 `0x0b` / `0x1e`）让写格抛 `IllegalCharacterError`
    ⇒ 整份导出 500。E-0243 的「上游自由文本当不可信输入」2026-10-16 只落在了绩效导出
    （`performance_export.safe_cell_value`），9-27 写的订单导出没带走。

    判据打在两个真实出口：① HTTP **200**（修前这条直接 500 / 用例报错）；② **单元格值**
    已清洗（`\x0b` / `\x1e` 剔掉），且**合法**字符（`\\n`）逐字保留——防「净化过头」。
    """

    def setUp(self):
        self.game = Game.objects.create(name="清洗游戏甲")
        # `GameServer.game` 是非空 FK ⇒ 建时必须带上所属游戏（同文件 setUp 的口径）。
        self.server = GameServer.objects.create(game=self.game, name="清洗区服乙")
        self.publisher = User.objects.create_user(username="清洗发单人丙", password="x")
        # 端点腿：导出闸 = `order.export` 节点（节点挂在 Group → Role 上 ⇒ 必须给组，
        # 见 `test_order_export_api.py` 模块 docstring 第 1 条的实测）。
        self.client = make_logged_in_client(
            username="清洗导出丁", groups=("客服",), nodes=("order.export",),
        )

    def _order(self, order_no, **overrides):
        base = dict(
            order_no=order_no,
            source_order_no="",
            source="清洗来源",
            game=self.game,
            server=self.server,
            hall_publisher=self.publisher,
            status=OrderStatus.IN_PROGRESS,
            content="清洗内容",
            # 脏字符就埋在这两列（E-0249 点名的两列；`0x0b` 在 remark、`0x1e` 在 project_text），
            # `\\n` 是**合法**字符 ⇒ 必须逐字保留（防净化过头的反向判据）。
            remark="备注甲\x0b备注乙\n备注丙",
            project_text="项目甲\x1e项目乙",
            total_price=Decimal("10.00"),
            refund_fee=Decimal("0.00"),
        )
        base.update(overrides)
        return Order.objects.create(**base)

    def test_export_is_200_and_dirty_cells_are_sanitized(self):
        """含 `\\x0b` / `\\x1e` 的合成单进导出 ⇒ 200，且两列的格 = 原文剔掉脏字符。

        期望值全是**字面量**（E-0024）；列下标按 31 列表的 0-based 位次：
        第 9 列「重要备注」= `remark`、第 10 列「代练/陪玩项目」= `project_text`。
        """
        self._order("20261018000000000001")
        response = self.client.get("/api/business/orders/export")
        # 修前这条是红的：`IllegalCharacterError` 从视图里冒出来 ⇒ 用例在上一行就报错。
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response["Content-Type"], (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ))
        sheet = workbook_of(response)["Sheet1"]
        # 只有一行数据 ⇒ 数据行就是第 2 行；先钉订单号确认读的是这一行。
        self.assertEqual(sheet.cell(row=2, column=2).value, "20261018000000000001")
        self.assertEqual(sheet.cell(row=2, column=9).value, "备注甲备注乙\n备注丙")
        self.assertEqual(sheet.cell(row=2, column=10).value, "项目甲项目乙")


class OrderExportPeopleNameApiTests(TenantTestCase):
    """端到端：导出表两个**人名列**出的是**显示名**，不是登录账号（2026-11-05 业务方）。

    第 23 列「发单人」= FK 上的 `user_display_name`（昵称优先、空串回落账号）；
    第 11 列「录单人」= 视图批量映射（`display_names_for_usernames`，一次 `username__in`）
    ——`created_by` 存的是账号快照（下游按账号消费，存储值不动），只在导出这一层换显示名。
    判据打在**视图真出口**（xlsx 单元格）：视图漏算 / 漏传映射 ⇒ 对应列回落成登录账号 ⇒ 红。
    纯函数侧的两档（映射 / 回落）钉在 `OrderExportWorkbookTests` 与 31 格主用例里。
    """

    def setUp(self):
        self.game = Game.objects.create(name="人名导出游戏甲")
        # `GameServer.game` 是非空 FK ⇒ 建时必须带上所属游戏（同文件 setUp 的口径）。
        self.server = GameServer.objects.create(game=self.game, name="人名导出区服乙")
        # 两列各一个「昵称 ≠ 账号」的真用户 ⇒ 读到账号的实现必红。
        self.publisher = User.objects.create_user(
            username="人名导出发单账号甲", first_name="人名导出发单昵称乙", password="x"
        )
        self.recorder = User.objects.create_user(
            username="人名导出录单账号丙", first_name="人名导出录单昵称丁", password="x"
        )
        self.client = make_logged_in_client(
            username="人名导出操作戊", groups=("客服",), nodes=("order.export",),
        )

    def test_both_name_cells_are_display_names(self):
        Order.objects.create(
            order_no="20261105000000000001",
            game=self.game,
            server=self.server,
            hall_publisher=self.publisher,
            created_by=self.recorder.username,
            status=OrderStatus.IN_PROGRESS,
        )
        response = self.client.get("/api/business/orders/export")
        self.assertEqual(response.status_code, 200, response.content[:200])
        sheet = workbook_of(response)["Sheet1"]
        # 先钉订单号确认读的是这一行，再钉两个人名列。
        self.assertEqual(sheet.cell(row=2, column=2).value, "20261105000000000001")
        self.assertEqual(sheet.cell(row=2, column=11).value, "人名导出录单昵称丁")
        self.assertEqual(sheet.cell(row=2, column=23).value, "人名导出发单昵称乙")

    def test_created_by_cell_falls_back_when_account_is_unknown(self):
        """查不到的账号（历史快照 / 已硬删）回落原值——不空格、不 500。"""
        Order.objects.create(
            order_no="20261105000000000002",
            game=self.game,
            server=self.server,
            created_by="人名导出已消失账号",
            status=OrderStatus.IN_PROGRESS,
        )
        response = self.client.get("/api/business/orders/export")
        self.assertEqual(response.status_code, 200, response.content[:200])
        sheet = workbook_of(response)["Sheet1"]
        self.assertEqual(sheet.cell(row=2, column=11).value, "人名导出已消失账号")


class OrderExportFormulaEscapeTests(TenantTestCase):
    """D-0334（二轮 Z13 / 五轮 M-08：xlsx 公式注入）端到端回归钉。

    业务方拍板口径：**导出边界转义**——写单元格前，文本若以
    `=` / `+` / `-` / `@`（表格公式起始符）开头 ⇒ 前缀 `'`；⛔ 数字 / 日期 /
    普通中文完全原样、⛔ 不动导入、⛔ 不动屏幕显示。

    修前形状（openpyxl 3.1.5 `Cell._bind_value` 读源码 + 探针实锤）：`len > 1` 且以
    `=` 开头的字符串写格即被绑成**公式**（`data_type == 'f'`）⇒ `data_only=True`
    读回 `None`（文本静默丢失），在 Excel 里还会被求值。`+` / `-` / `@` 在 xlsx
    本身不触发公式，但属拍板集合（防「另存 CSV 再打开」一类下游求值）⇒ 一并转义。
    """

    def setUp(self):
        self.game = Game.objects.create(name="转义游戏甲")
        # `GameServer.name` 故意以 `-` 开头：`-` 是拍板的四个起始符之一（区服名是自由文本）。
        # `GameServer.game` 是非空 FK ⇒ 建时必须带上所属游戏（同文件 setUp 的口径）。
        self.server = GameServer.objects.create(game=self.game, name="-区服乙")
        self.publisher = User.objects.create_user(username="转义发单人丙", password="x")
        # 端点腿：导出闸 = `order.export` 节点（同 `OrderExportSanitizeApiTests` 的形状；
        # `make_logged_in_client` 默认给当前租户全部游戏，D-0136 ⇒ 不必另授游戏）。
        self.client = make_logged_in_client(
            username="转义导出丁", groups=("客服",), nodes=("order.export",),
        )

    def _order(self, **overrides):
        base = dict(
            order_no="20261030000000000001",
            source_order_no="",
            source="转义来源",
            game=self.game,
            server=self.server,
            hall_publisher=self.publisher,
            status=OrderStatus.IN_PROGRESS,
            content="=1+1",
            remark="+备注开头丙",
            project_text="转义项目戊",
            customer_ww="@旺旺己",
            game_role_name="转义角色庚",
            total_price=Decimal("10.00"),
            refund_fee=Decimal("-2.50"),
        )
        base.update(overrides)
        return Order.objects.create(**base)

    def _sheet(self):
        response = self.client.get("/api/business/orders/export")
        # 修前 `=1+1` 成公式不会 500（写格照常成功）⇒ 200 不是本组判据，真正的判据在格上。
        self.assertEqual(response.status_code, 200, response.content[:200])
        return workbook_of(response)["Sheet1"]

    def test_formula_leading_texts_land_as_text_cells(self):
        """四个起始符各占一列 ⇒ 全部 `'` 前缀 + `data_type == 's'`。

        列下标（31 列表的 1 起位次）：5 服务器、6 横排内容、7 内容、9 重要备注、30 旺旺。
        修前：第 6/7 列被绑成公式（`data_type == 'f'`、`data_only=True` 读回 `None`）⇒ 红。
        """
        self._order()
        sheet = self._sheet()

        expectations = {
            5: "'-区服乙",        # 服务器（`GameServer.name`）
            6: "'=1+1",           # 格式化订单内容横排（content 无换行 ⇒ 与第 7 列同文）
            7: "'=1+1",           # 格式化订单内容（content）
            9: "'+备注开头丙",    # 重要备注（remark）
            30: "'@旺旺己",       # 旺旺（customer_ww）
        }
        for column, expected in expectations.items():
            with self.subTest(column=column):
                cell = sheet.cell(row=2, column=column)
                self.assertEqual(cell.data_type, "s")
                self.assertEqual(cell.value, expected)

    def test_numeric_and_normal_cells_are_untouched(self):
        """回归钉（零行为意外）：负数金额是**数值格**（⛔ 不许被加前缀）、普通中文逐字不变。

        ⚠️ `refund_fee = -2.50` 是故意的：证明转义只认**文本**格，金额照旧走数值
        （D-0015：金额 0 与负数都是合法且有意义的边界）。
        """
        self._order()
        sheet = self._sheet()

        price = sheet.cell(row=2, column=15)          # 接单价 = total_price
        refund = sheet.cell(row=2, column=17)         # 退款 = refund_fee（本例为负数）
        role = sheet.cell(row=2, column=14)           # 角色名 = game_role_name
        project = sheet.cell(row=2, column=10)        # 代练/陪玩项目 = project_text

        self.assertEqual(price.data_type, "n")
        self.assertEqual(Decimal(str(price.value)), Decimal("10.00"))
        self.assertEqual(refund.data_type, "n")
        self.assertEqual(Decimal(str(refund.value)), Decimal("-2.50"))
        self.assertEqual(role.data_type, "s")
        self.assertEqual(role.value, "转义角色庚")
        self.assertEqual(project.value, "转义项目戊")


#: 生产代码（`src/apps/**/*.py`）里 import 了 openpyxl 的模块——**显式枚举**，
#: ⛔ 不从下面的扫描结果派生（派生 = 自指 = 恒真，E-0024）。
PRODUCTION_OPENPYXL_LANDING_POINTS = frozenset({
    "apps/business/order_export.py",
    "apps/business/performance_export.py",
    # ⭐ D-0365（我的订单收入导出）：第三个落点（单工作表「收入记录」，
    # 构造照 `performance_export.py` 的形状：openpyxl 只在 `build_workbook` 里使用，
    # 视图不碰）。
    "apps/business/my_orders_export.py",
})

#: 「这个文件用 openpyxl」的判据 = 文件里有一行 openpyxl 的顶层 import。
_OPENPYXL_IMPORT = re.compile(r"^\s*(?:import openpyxl\b|from openpyxl(?:\.\w+)* import\b)", re.MULTILINE)


class ProductionOpenpyxlLandingPointTests(SimpleTestCase):
    """生产代码里 openpyxl 的**落点枚举网**（2026-09-27 订单导出线 T7 收口补网）。

    **为什么有这条**：`performance_export.py` 的模块 docstring 原写「本仓只有这一处 openpyxl
    落点」——本线新增第二个落点（订单导出模块）后那句被证伪。E-0011 的处置不是改个词了事，
    而是把口径换成**可复核的枚举**：`src/apps/**/*.py` 里 import 了 openpyxl 的文件**恰好**
    是上面那两个（本网钉住这个集合；加第三个 / 少一个都红）。

    **编号**：本线收口新增，spec §8-1 的表只到 B19 ⇒ 当场取 **B20**（⛔ 不是 spec 原有条目）。

    ⚠️ 只扫**生产代码**（`src/apps/`，不含 `src/tests/`）：测试侧本来就用 openpyxl 读回产物
    （`tests/xlsx_helpers.py` 等），把它算进「落点」会让这条判据失去意义。
    """

    # ⭐ D-0365：本网方法名原为
    #    `test_exactly_the_two_enumerated_modules_import_openpyxl`——第三个落点
    #    （`my_orders_export.py`）加入后「two」就是一句假话，按本仓改名先例
    #    （`test_no_route_500` 的「the_two_shared_converter_routes」同型）改掉。
    def test_exactly_the_enumerated_modules_import_openpyxl(self):
        apps_root = Path(__file__).resolve().parents[1] / "apps"
        found = set()
        for path in sorted(apps_root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if _OPENPYXL_IMPORT.search(path.read_text(encoding="utf-8")):
                found.add(path.relative_to(apps_root.parent).as_posix())
        self.assertEqual(found, PRODUCTION_OPENPYXL_LANDING_POINTS)


class IncomeAndPerformanceExportNameTests(TenantTestCase):
    """收入记录 / 绩效明细两个导出的第 1 列「用户(姓名/)名称」也出**显示名**（2026-11-05 业务方）。

    与 OrderExportPeopleNameApiTests 同款「昵称 ≠ 账号」夹具：读到登录账号的实现必红。
    纯函数侧直拍（my_orders_export.row_values / performance_export.detail_values 都是纯映射）。
    """

    def setUp(self):
        self.game = Game.objects.create(name="收入导出游戏甲")
        self.server = GameServer.objects.create(game=self.game, name="收入导出区服乙")
        self.taker = User.objects.create_user(
            username="收入导出账号甲", first_name="收入导出昵称乙", password="x"
        )
        self.order = Order.objects.create(
            order_no="20261105000000000003",
            game=self.game,
            server=self.server,
            status=OrderStatus.IN_PROGRESS,
        )
        self.performance = Performance.objects.create(
            order=self.order, user=self.taker,
        )

    def test_income_export_user_cell_is_display_name(self):
        row = my_orders_export.row_values(self.performance)
        self.assertEqual(row[0], "收入导出昵称乙")

    def test_performance_detail_user_cell_is_display_name(self):
        performance = Performance.objects.select_related(
            "order__game", "order__order_source"
        ).get(pk=self.performance.pk)
        row = performance_export.detail_values(performance, can_view_offer_price=True)
        self.assertEqual(row[0], "收入导出昵称乙")
