"""：绩效导出改成附件那种 Excel（`.xlsx`）。

    **需求出处**： brief §5.1–§5.4 + 文末「控制器补充（附件单元格的存储类型与断言写法）」。
    附件（业务方自己从对标产品导出的 `效绩提成记录0923055917.xlsx`）是**唯一**的列集/表头证据：
    两个工作表 `绩效明细`（29 列）/ `绩效汇总`（10 列），顺序与表头逐字照抄。

    **2026-10-16 追加**（spec `docs/specs/2026-10-16-alipay-payment-sheet-and-export-sanitize-design.md`）：
    ① 写格前剔掉 XML 非法控制字符（修 E-0242，`项目` 带 `0x1e` 会让整份导出 500）；
    ② 底部再加两张副表——第 3 张 `页一支付宝批量付款文件上传模板`（照支付宝官方模板的「打款表」，
    一人一行、金额 = Σ `bonus_amount`）与第 4 张 `打款跳过名单`（谁没被打款、为什么）。
    ⇒ 工作表**恰好 4 张**、顺序固定（本文件的表名断言已同步改成 4 个字面量）。

    本文件覆盖：

    - 工作表名与顺序；29 / 10 个表头**逐字**（期望值写**字面列表**，⛔ 不引被测模块的
    `DETAIL_HEADERS`/`SUMMARY_HEADERS`——自指断言在常量被改坏时不会红，E-0024）；
    - 一行的 29 格逐格钉值：金额是**数值格**（附件实测如此），时间列是 `"MM-DD HH:MM"` 字符串；
    - 汇总按**用户名分组**、四项求和、行序**用户名升序**、`StaffProfile` 取不到 ⇒ 空；
    - `发单价` 的 `order.restrict_view_offer_price` **脱敏分支**（空白 / 正常两向对照）；
    - 需求 A 的三条（净化成功 / 逐字保留 / `\t` `\n` `\r` 不在净化面）；
    - 需求 B 的两张副表（表头逐字、一人一行、金额数值格与两位小数、两种结账状态都参与合计、
    三种跳过原因正反两面）；
    - 超上限**仍回统一信封 + HTTP 200**（报错不截断、不给半截文件）；
    - 响应头：xlsx MIME + ASCII 兜底名 + RFC 5987 `filename*`（中文名）。

    ⚠️ **数值格的断言写法**（控制器补充，2026-09-23 实测 openpyxl 3.1.5）：写
    `Decimal("123.00")` 读回是 **`int` 123**、写 `Decimal("4.40")` 读回是 **`float` 4.4**
    ⇒ `assertEqual(cell.value, Decimal("4.40"))` 会**假红**（`4.4 != Decimal("4.40")`）。
    本文件一律写成 `Decimal(str(cell.value)) == Decimal("4.40")`。

    ⚠️ **断言打在真实出口**：一律 `load_workbook(BytesIO(response.content))` **读回字节**
    （不直接调 `detail_values` / `build_workbook` 的内部结果当日志），并同时断言金额格
    **不是 `str`**——「金额写成了字符串」正是附件证据要防的那件事。
"""
import re
from datetime import datetime
from decimal import Decimal
from unittest import mock
from urllib.parse import unquote

from django.contrib.auth.models import User
from django.test import SimpleTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderSource,
    OrderStatus,
    Performance,
    ReviewStatus,
    SettleStatus,
)
from apps.business.performance_export import detail_values, safe_cell_text
from apps.business.performance_export import safe_cell_value, summary_values
from tests.p5_auth_helpers import grant_games, make_logged_in_client
from tests.xlsx_helpers import detail_rows as read_detail_rows
from tests.xlsx_helpers import summary_rows as read_summary_rows
from tests.xlsx_helpers import workbook_of

EXPORT_URL = "/api/business/performances/export"

#: 节点 code 写字面值（E-0024：不引被测模块导出的常量）。
NODE_EXPORT = "finance.export_performance"
NODE_RESTRICT_OFFER_PRICE = "order.restrict_view_offer_price"

#: xlsx 的官方 MIME（brief §5.1 逐字）。
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: 附件的 29 个明细表头（**字面副本**，逐字来自附件 `绩效明细` 第 1 行）。
EXPECTED_DETAIL_HEADERS = [
    "用户姓名/名称",
    "预计额",
    "实计额",
    "订单价格",
    "退款",
    "提成类型",
    "关联订单",
    "接手人",
    "发单价",
    "系统内置单号",
    "游戏名称",
    "订单角色名",
    "职业",
    "项目",
    "订单来源",
    "上传截图数",
    "排团编号",
    "接单客服",
    "订单状态",
    "淘宝状态",
    "收款状态",
    "旺旺",
    "记录时间",
    "提交时间",
    "记录人",
    "清算状态",
    "清算时间",
    "录单时间",
    "账单备注",
]

#: 附件的 10 个汇总表头（**字面副本**，逐字来自附件 `绩效汇总` 第 1 行）。
EXPECTED_SUMMARY_HEADERS = [
    "用户姓名/名称",
    "总计接单数",
    "总计预计额",
    "总计实计额",
    "总计订单价",
    "总计退款",
    "结算账号",
    "结算账号户名",
    "结算银行名称",
    "结算银行卡号",
]

#: 第 3 张副表的表名 / 第 1 行 / 第 2 行五个表头（**字面副本**，逐字来自业务方给的
#: 支付宝官方模板 `.tools/recon/alipay-payment-template/alipay-batch-payment-template.xls`；
#: ⛔ 不引被测模块的 `ALIPAY_*` 常量，E-0024）。
PAYMENT_SHEET = "页一支付宝批量付款文件上传模板"
PAYMENT_TITLE_ROW = "支付宝批量付款文件模板（前面两行请勿删除）"
EXPECTED_PAYMENT_HEADERS = [
    "序号（必填）",
    "收款方支付宝账号（必填）",
    "收款方姓名（必填）",
    "金额（必填，单位：元）",
    "备注（选填）",
]

#: 第 4 张副表（本仓自定）的表名与 5 个表头（同上，字面副本）。
SKIPPED_SHEET = "打款跳过名单"
EXPECTED_SKIPPED_HEADERS = [
    "用户姓名/名称",
    "结算支付宝账号",
    "结算支付宝姓名",
    "跳过原因",
    "未打款金额",
]


class _ExportFixture(TenantTestCase):
    """建单 / 建绩效的小夹具（各用例只表达**自己的差异**）。"""

    #: 非超管 client 的专属小组序号（`Role.name` 唯一且 ≤5 字 ⇒ 同组会共享节点）。
    _GROUP_SEQ = 0

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # 管理员登录：`has_node_now` / `flag_enabled` 的租户管理员旁路都对他短路
        # ⇒ 业务形状的用例不必先搭角色目录。
        self.finance = make_logged_in_client(username="xlsx-fin", is_superuser=True)
        self.taker = User.objects.create_user(username="xlsx-taker")
        self._seq = 0

    def _client(self, username, *codes):
        """真实登录 + **专属节点组**（同组会共享节点 ⇒ 必须每次一个新组名）。

    非管理员的读路径按游戏权限收窄，必须显式授权本用例的游戏，
    否则导出只剩表头（`detail_rows(...)[1]` 会 `IndexError`）。
    `self.finance` 是超管（租户管理员旁路），不受这条影响。
"""
        type(self)._GROUP_SEQ += 1
        group = f"X{type(self)._GROUP_SEQ}"            # 2 个字符，不超过 5
        client = make_logged_in_client(username=username, groups=(group,), nodes=codes)
        grant_games(User.objects.get(username=username), self.game)
        return client

    def _order(self, **overrides):
        self._seq += 1
        fields = {
            "game": self.game,
            # 20 位订单号（`Order.order_no` 的列宽）。
            "order_no": f"2026092300000{self._seq:07d}",
            "status": OrderStatus.COMPLETED,
            "hall_price": Decimal("100.00"),
        }
        fields.update(overrides)
        return Order.objects.create(**fields)

    def _perf(self, order, *, user, **overrides):
        fields = {
            "emp_type": EmpType.MAIN,
            "temp_amount": Decimal("25.00"),
            "bonus_amount": Decimal("25.00"),
            # `performance_review_needs_user` 这条 CHECK 是
            # `review_status IS NULL OR user IS NOT NULL`，即「**有审核状态 ⇒ 有归属人**」
            # （反向不成立：本行的夹具正是「有归属人 + 无审核状态」的合法形态）。
            "review_status": ReviewStatus.APPROVED,
            "settle_status": SettleStatus.UNSETTLED,
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, user=user, **fields)

    def _workbook(self, client=None, params=None):
        """发一次导出、断形状与 MIME，再把字节读回成 openpyxl 工作簿。"""
        response = (client or self.finance).get(EXPORT_URL, params or {})
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response["Content-Type"], XLSX_MIME, response["Content-Type"])
        return workbook_of(response)

    def _detail_rows(self, workbook):
        return read_detail_rows(workbook)

    def _summary_rows(self, workbook):
        return read_summary_rows(workbook)

    def _payment_rows(self, workbook):
        """打款表的**数据行**（第 3 行起）：每行 5 个格值（`序号` / 账号 / 姓名 / 金额 / 备注）。"""
        sheet = workbook[PAYMENT_SHEET]
        return [
            tuple(sheet.cell(row=index, column=column).value for column in range(1, 6))
            for index in range(3, sheet.max_row + 1)
        ]

    def _skipped_rows(self, workbook):
        """跳过名单的**数据行**（第 2 行起）：每行 5 个格值（含 `跳过原因` / `未打款金额`）。"""
        sheet = workbook[SKIPPED_SHEET]
        return [
            tuple(sheet.cell(row=index, column=column).value for column in range(1, 6))
            for index in range(2, sheet.max_row + 1)
        ]

    def _profile(self, user, *, account, name):
        return StaffProfile.objects.create(
            user=user, alipay_account=account, alipay_name=name
        )

    def _assert_money(self, cell_value, expected):
        """金额格：**数值**（⛔ 不是 `str`）+ 值相等（`Decimal(str(...))`，见模块 docstring）。"""
        self.assertNotIsInstance(cell_value, str, f"金额格应当是数值：{cell_value!r}")
        self.assertEqual(Decimal(str(cell_value)), Decimal(expected))

    def _assert_blank(self, cell_value):
        """空格：写进去的是空串，openpyxl 落成**真空单元格** ⇒ 读回是 `None`。

    ⚠️ 附件里「空」的格是**空共享字符串**（读回 `''`）——两者在 Excel 里都是空格；
    brief 的补充说明只钉了「金额是数值格 / 时间是共享字符串」，**没有**要求空格的
    存储类型逐字节同构。所以这里接受 `None` 或 `''`（⛔ 不接受 `0` / `"None"` / 别的值）。
"""
        self.assertIn(cell_value, (None, ""), f"应当是空格：{cell_value!r}")


class SheetAndHeaderTests(_ExportFixture):
    """四个工作表与 29 / 10 个表头逐字（顺序也是判据）。"""

    def test_sheets_and_headers_are_verbatim_from_the_reference(self):
        self._perf(self._order(), user=self.taker)

        workbook = self._workbook()

        # ⭐ **恰好 4 张、顺序固定**（字面列表，⛔ 不放宽成 `toContain`、
        # ⛔ 不引被测模块的表名常量）——后两张是本批新增的副表。
        self.assertEqual(
            workbook.sheetnames,
            [
                "绩效明细",
                "绩效汇总",
                "页一支付宝批量付款文件上传模板",
                "打款跳过名单",
            ],
        )
        detail = self._detail_rows(workbook)
        self.assertEqual(len(detail[0]), 29)
        self.assertEqual(detail[0], EXPECTED_DETAIL_HEADERS)
        summary = self._summary_rows(workbook)
        self.assertEqual(len(summary[0]), 10)
        self.assertEqual(summary[0], EXPECTED_SUMMARY_HEADERS)


class DetailRowTests(_ExportFixture):
    """一行的 29 格逐格钉值（含「金额是数值格」「时间列是 MM-DD HH:MM 字符串」）。"""

    def test_detail_row_carries_the_mapped_values_with_numeric_money(self):
        source = OrderSource.objects.create(name="来源甲")
        order = self._order(
            total_price=Decimal("268.00"),
            refund_fee=Decimal("8.00"),
            hall_price=Decimal("120.00"),
            source_order_no="进3316400726004017083",
            game_role_name="角色甲",
            project_text="原神代练",
            customer_ww="ww-abc",
            sales_agent="客服甲",
            created_by="录入员甲",
            order_source=source,
        )
        performance = self._perf(
            order,
            user=self.taker,
            temp_amount=Decimal("25.00"),
            bonus_amount=Decimal("20.00"),
        )
        # `created_at` 是 `auto_now_add` ⇒ 建完再改到一个确定的本地时刻（导出按本地时区格式化）。
        created = timezone.make_aware(datetime(2026, 9, 23, 14, 5))
        Performance.objects.filter(pk=performance.pk).update(created_at=created)
        Order.objects.filter(pk=order.pk).update(created_at=created)

        row = self._detail_rows(self._workbook())[1]

        self.assertEqual(len(row), 29)
        self.assertEqual(row[0], "xlsx-taker")
        self._assert_money(row[1], "25.00")          # 预计额
        self._assert_money(row[2], "20.00")          # 实计额
        self._assert_money(row[3], "268.00")         # 订单价格
        self._assert_money(row[4], "8.00")           # 退款
        self.assertEqual(row[5], "打单工资")           # 提成类型（库里的中文值）
        self.assertEqual(row[6], "进3316400726004017083")   # 关联订单
        self._assert_blank(row[7])                   # 接手人：无数据源 ⇒ 空
        self._assert_money(row[8], "120.00")         # 发单价（未受限）
        self.assertEqual(row[9], order.order_no)     # 系统内置单号
        self.assertEqual(row[10], "原神")             # 游戏名称
        self.assertEqual(row[11], "角色甲")           # 订单角色名
        self._assert_blank(row[12])                  # 职业：无数据源
        self.assertEqual(row[13], "原神代练")          # 项目
        self.assertEqual(row[14], "来源甲")           # 订单来源（字典名）
        self._assert_blank(row[15])                  # 上传截图数：无数据源
        self._assert_blank(row[16])                  # 排团编号：无数据源
        self.assertEqual(row[17], "客服甲")           # 接单客服
        self.assertEqual(row[18], "已完成")           # 订单状态：中文 label
        self._assert_blank(row[19])                  # 淘宝状态：无数据源
        self._assert_blank(row[20])                  # 收款状态：无数据源
        self.assertEqual(row[21], "ww-abc")          # 旺旺
        self.assertEqual(row[22], "09-23 14:05")     # 记录时间（本地时区）
        self._assert_blank(row[23])                  # 提交时间：无数据源
        self.assertEqual(row[24], "录入员甲")          # 记录人
        self.assertEqual(row[25], "未结账")           # 清算状态
        self._assert_blank(row[26])                  # 清算时间：未结账 ⇒ 空
        self.assertEqual(row[27], "09-23 14:05")     # 录单时间（订单 created_at）
        self._assert_blank(row[28])                  # 账单备注：无数据源

    def test_order_source_falls_back_to_the_source_snapshot(self):
        """`order.order_source` 为空 ⇒ 回落 `order.source`（字典化之前的文本快照）。"""
        order = self._order(source="淘宝", order_source=None)
        self._perf(order, user=self.taker)

        row = self._detail_rows(self._workbook())[1]

        self.assertEqual(row[14], "淘宝")

    def test_settled_row_carries_the_settlement_time(self):
        """已结账 ⇒ 清算状态中文 + 清算时间 `MM-DD HH:MM`；未结账那一格是空（上面那条已钉）。"""
        performance = self._perf(
            self._order(), user=self.taker, settle_status=SettleStatus.SETTLED
        )
        settled = timezone.make_aware(datetime(2026, 9, 23, 9, 7))
        Performance.objects.filter(pk=performance.pk).update(settled_at=settled)

        row = self._detail_rows(self._workbook())[1]

        self.assertEqual(row[25], "已结账")
        self.assertEqual(row[26], "09-23 09:07")


class SummarySheetTests(_ExportFixture):
    """`绩效汇总`：按用户名分组、四项求和、行序用户名升序、档案缺 ⇒ 空。"""

    def test_summary_groups_by_username_sums_and_sorts_ascending(self):
        # 用户名**倒序**创建：若按插入/主键排序，zz 会排在 aa 前面 ⇒ 这条能分辨两种实现。
        zz = User.objects.create_user(username="zz-taker")
        aa = User.objects.create_user(username="aa-taker")
        StaffProfile.objects.create(
            user=zz,
            alipay_account="b@example.com",
            alipay_name="乙",
            bank_name="",
            bank_card_no="",
        )
        # zz 两行（合计：2 单 / 预计 40 / 实计 60 / 订单价 300 / 退款 5）
        self._perf(
            self._order(total_price=Decimal("100.00"), refund_fee=Decimal("5.00")),
            user=zz,
            temp_amount=Decimal("30.00"),
            bonus_amount=Decimal("20.00"),
        )
        self._perf(
            self._order(total_price=Decimal("200.00"), refund_fee=Decimal("0.00")),
            user=zz,
            temp_amount=Decimal("10.00"),
            bonus_amount=Decimal("40.00"),
        )
        # aa 一行（退款式负数：D-0015 之下照实求和）
        self._perf(
            self._order(total_price=Decimal("50.00"), refund_fee=Decimal("-2.00")),
            user=aa,
            temp_amount=Decimal("5.00"),
            bonus_amount=Decimal("5.00"),
        )

        rows = self._summary_rows(self._workbook())

        self.assertEqual([row[0] for row in rows[1:]], ["aa-taker", "zz-taker"])
        aa_row, zz_row = rows[1], rows[2]

        self.assertEqual(aa_row[1], 1)
        self._assert_money(aa_row[2], "5.00")
        self._assert_money(aa_row[3], "5.00")
        self._assert_money(aa_row[4], "50.00")
        self._assert_money(aa_row[5], "-2.00")
        # 没有 `StaffProfile` ⇒ 四列都空（不编造、也不借别人的档案）。
        for cell in aa_row[6:10]:
            self._assert_blank(cell)


        self.assertEqual(zz_row[1], 2)
        self._assert_money(zz_row[2], "40.00")
        self._assert_money(zz_row[3], "60.00")
        self._assert_money(zz_row[4], "300.00")
        self._assert_money(zz_row[5], "5.00")
        self.assertEqual(zz_row[6], "b@example.com")
        self.assertEqual(zz_row[7], "乙")
        self._assert_blank(zz_row[8])
        self._assert_blank(zz_row[9])

    def test_a_two_row_order_keeps_price_and_refund_once_per_user_bucket(self):
        """D-0255（2026-10-18）：导出汇总的「总计订单价 / 总计退款」也**按单去重**（同一次改）。

    一张「打手+客服」两行单：**各自的桶**各计一次价与退款（打手行 100/20、客服行
    100/20）。⚠️ 如实登记：`(order, user)` 唯一约束让一个桶里一张单今天最多一行
    ⇒ 本桶内去重是**结构性保险**，构造不出「桶内同单两行」的红；本条钉的是
    **不误伤**——每桶首见的那一行照常把价/退款计入（去重写过头把合法行也丢了
    ⇒ 本条的 100/20 必红）。与 summary 端点那条钉（`test_emp_emplist_backend.py`
    的 `test_a_two_row_order_counts_price_and_refund_once_per_order`）同一裁定、
    两个出口各自钉。
"""
        zz = User.objects.create_user(username="zz-two-row")
        aa = User.objects.create_user(username="aa-two-row")
        order = self._order(total_price=Decimal("100.00"), refund_fee=Decimal("20.00"))
        self._perf(
            order, user=zz,
            temp_amount=Decimal("30.00"), bonus_amount=Decimal("30.00"),
        )
        self._perf(
            order, user=aa, emp_type=EmpType.SALES,
            temp_amount=Decimal("10.00"), bonus_amount=Decimal("10.00"),
        )

        rows = self._summary_rows(self._workbook())

        self.assertEqual([row[0] for row in rows[1:]], ["aa-two-row", "zz-two-row"])
        aa_row, zz_row = rows[1], rows[2]
        # 客服行（SALES）：1 行 / 10 / 10 / 价 100 一次 / 退款 20 一次
        self.assertEqual(aa_row[1], 1)
        self._assert_money(aa_row[2], "10.00")
        self._assert_money(aa_row[3], "10.00")
        self._assert_money(aa_row[4], "100.00")
        self._assert_money(aa_row[5], "20.00")
        # 打手行（MAIN）：同上，价与退款在本桶也只随这一行计一次
        self.assertEqual(zz_row[1], 1)
        self._assert_money(zz_row[2], "30.00")
        self._assert_money(zz_row[3], "30.00")
        self._assert_money(zz_row[4], "100.00")
        self._assert_money(zz_row[5], "20.00")


class DisplayNameSweepTests(_ExportFixture):
    """2026-11-05 业务方「所有导出文件、含副表，人名列全部改显示名」——绩效导出的
    三个剩余漏点：汇总第 1 列、跳过名单第 1 列、明细第 25 列「记录人」（此前原样出
    登录账号）。夹具同款「昵称 ≠ 账号」：读到登录账号的实现必红。
    ⚠️ 分组 / 行序的键仍是登录账号（只换输出格）——各条断言都顺带钉住这一点。
"""

    def setUp(self):
        super().setUp()
        self.named = User.objects.create_user(
            username="清扫绩效账号甲", first_name="清扫绩效昵称乙", password="x"
        )

    def test_summary_first_cell_is_display_name_while_rows_stay_grouped_by_username(self):
        """① 汇总表第 1 格 = 显示名；行序仍按**用户名**升序（`xlsx-taker` < 中文账号），
        仍是**两行**（按显示名分组/排序会红）。"""
        self._perf(self._order(), user=self.taker)
        self._perf(self._order(), user=self.named)

        rows = self._summary_rows(self._workbook())

        self.assertEqual(
            [row[0] for row in rows[1:]], ["xlsx-taker", "清扫绩效昵称乙"]
        )

    def test_skip_list_first_cell_is_display_name(self):
        """② 跳过名单第 1 格 = 显示名（没档案 ⇒ 两条原因各一行，原因序不变）。"""
        self._perf(self._order(), user=self.named)

        rows = self._skipped_rows(self._workbook())

        self.assertEqual(
            [(row[0], row[3]) for row in rows],
            [
                ("清扫绩效昵称乙", "没填结算支付宝账号"),
                ("清扫绩效昵称乙", "没填结算支付宝姓名"),
            ],
        )

    def test_detail_recorder_cell_maps_display_name_and_falls_back_for_unknown_accounts(self):
        """③ 明细第 25 格「记录人」= 映射出的显示名；映射查不到的账号回落**原值**
        （账号快照仍在库里，只换导出格）。行序 = `-created_at, -id` ⇒ 后建的那行在前。"""
        unmapped = self._perf(self._order(created_by="已硬删的账号乙"), user=self.taker)
        mapped = self._perf(self._order(created_by="清扫绩效账号甲"), user=self.taker)
        assert unmapped.pk < mapped.pk

        rows = self._detail_rows(self._workbook())

        self.assertEqual(rows[1][24], "清扫绩效昵称乙")   # 映射命中 → 显示名
        self.assertEqual(rows[2][24], "已硬删的账号乙")   # 查不到 → 回落原值

    def test_detail_values_without_mapping_keeps_the_raw_snapshot(self):
        """`created_by_names=None`（老调用方 / 直拍，如
        `test_order_export_xlsx.IncomeAndPerformanceExportNameTests`）⇒ 第 25 格按原值输出。"""
        self._perf(self._order(created_by="清扫绩效账号甲"), user=self.taker)
        performance = Performance.objects.select_related(
            "order__game", "order__order_source"
        ).get()

        row = detail_values(performance, can_view_offer_price=True)

        self.assertEqual(row[24], "清扫绩效账号甲")

    def test_summary_values_falls_back_to_username_without_a_display_name(self):
        """`display_name` 缺省 / 空串 ⇒ 第 1 格回落 `username`
        （`display_names_for_usernames` 已含回落，这里钉的是「调用方漏传不炸」）。"""
        totals = {
            "count": 1,
            "temp_amount": Decimal("25.00"),
            "bonus_amount": Decimal("25.00"),
            "order_price": Decimal("100.00"),
            "refund_fee": Decimal("0.00"),
        }

        self.assertEqual(summary_values("某个账号", totals, None)[0], "某个账号")
        self.assertEqual(summary_values("某个账号", totals, None, "")[0], "某个账号")
        self.assertEqual(
            summary_values("某个账号", totals, None, "某个昵称")[0], "某个昵称"
        )


class IllegalCharacterSanitizeTests(_ExportFixture):
    """需求 A（修 E-0242）：一行里的非法控制字符不再让**整份导出**失败。

    E-0242 的现场：`项目`（`order.project_text`）里带了 `0x1e`，openpyxl 在写格时抛
    `IllegalCharacterError` ⇒ 整个导出 500（连没问题的行也拿不到）。
"""

    def test_illegal_control_characters_are_stripped_and_the_export_succeeds(self):
        """含 `0x1e` / `0x0b` 的行 ⇒ 导出**成功**，且该格 = 原文剔掉那两个字符。"""
        order = self._order(project_text="前\u001e中\u000b后")
        self._perf(order, user=self.taker)

        row = self._detail_rows(self._workbook())[1]   # 净化前这一步就抛异常 ⇒ 用例红

        self.assertEqual(row[13], "前中后")

    def test_text_without_illegal_characters_is_kept_verbatim(self):
        """不含非法字符的行 ⇒ **逐字不变**（防净化过头）。"""
        order = self._order(project_text="原神代练（双排）")
        self._perf(order, user=self.taker)

        row = self._detail_rows(self._workbook())[1]

        self.assertEqual(row[13], "原神代练（双排）")

    def test_tab_and_newline_are_not_in_the_sanitize_set(self):
        """净化面**恰好**是 `0x00–0x08` / `0x0b` / `0x0c` / `0x0e–0x1f`：`\\t`(`0x09`) 与
    `\\n`(`0x0a`) 是**合法**字符 ⇒ 必须原样出现在格里。"""
        kept = "甲\t乙\n丙"
        order = self._order(project_text=kept)
        self._perf(order, user=self.taker)

        row = self._detail_rows(self._workbook())[1]

        self.assertEqual(row[13], kept)

    def test_carriage_return_is_not_in_the_sanitize_set(self):
        """`\\r`(`0x0d`) 同样是**合法**字符 ⇒ 净化器不许删它。

    ⚠️ **这条判据的层级 = 净化器函数层**，⛔ 不是「读回的格」：**实测**
    写 `"甲\\t乙\\n丙\\r丁"` 后，xlsx 里的 `xl/worksheets/sheet1.xml` 的 `<t>` 是
    **字面 `\\r`**（写这一侧没动它），而 `load_workbook` 读回来是 `"甲\\t乙\\n丙\\n丁"`
    ——`\\r` 被换成了 `\\n`。那不是本仓的净化，是 **XML 的换行归一（解析器行为）**：
    任何 XML 解析器都会把字符数据里的字面 CR 归一成 LF。⇒ 拿「读回的格」断言 `\\r`
    会是一条**恒假**的判据（无论净化对不对都红），所以 `\\r` 只在这里断言。
    `\\t` / `\\n` 不受这条影响，走端到端的格断言（上一条用例）。
"""
        self.assertEqual(safe_cell_value("甲\r乙"), "甲\r乙")
        self.assertEqual(safe_cell_value("甲\t乙\n丙\r丁"), "甲\t乙\n丙\r丁")


class AlipayPaymentSheetTests(_ExportFixture):
    """需求 B 的第 3 张表（支付宝批量打款）：表名 / 前两行逐字 + 一人一行 + 金额数值格。"""

    def test_sheet_name_and_the_two_leading_rows_are_verbatim_from_the_template(self):
        """判据 ④：表名、第 1 行、第 2 行五个表头——**逐字**（字面副本）。"""
        self._profile(self.taker, account="taker@example.com", name="打手甲")
        self._perf(self._order(), user=self.taker)

        sheet = self._workbook()[PAYMENT_SHEET]

        self.assertEqual(sheet.cell(row=1, column=1).value, PAYMENT_TITLE_ROW)
        self.assertEqual(
            [sheet.cell(row=2, column=column).value for column in range(1, 6)],
            EXPECTED_PAYMENT_HEADERS,
        )

    def test_the_title_row_is_merged_across_the_five_columns(self):
        """模板第 1 行是**合并格 `A1:E1`** ⇒ 本表照做。

    判据打在 `merged_cells.ranges` 上（⛔ 不是「没报错就算」）。
"""
        self._profile(self.taker, account="taker@example.com", name="打手甲")
        self._perf(self._order(), user=self.taker)

        sheet = self._workbook()[PAYMENT_SHEET]

        self.assertIn("A1:E1", [str(rng) for rng in sheet.merged_cells.ranges])

    def test_the_first_data_row_is_row_three_and_the_sequence_is_an_integer(self):
        """判据 ⑤ + ⑫：数据从**第 3 行**开始，`序号` 是**整数**（⛔ 不是 `"1"` 字符串）。"""
        self._profile(self.taker, account="taker@example.com", name="打手甲")
        self._perf(self._order(), user=self.taker)

        sheet = self._workbook()[PAYMENT_SHEET]
        cell = sheet.cell(row=3, column=1)

        self.assertEqual(cell.value, 1)
        self.assertEqual(cell.data_type, "n")
        self.assertIsInstance(cell.value, int)

    def test_one_row_per_alipay_account_with_a_continuous_sequence(self):
        """判据 ⑥：同账号多行（含两个「人」共用一个账号）⇒ **只出一行**，`序号` 连续。"""
        other_a = User.objects.create_user(username="xlsx-pay-a")
        other_b = User.objects.create_user(username="xlsx-pay-b")
        self._profile(other_a, account="shared@example.com", name="共用乙")
        self._profile(other_b, account="shared@example.com", name="共用乙")
        self._profile(self.taker, account="taker@example.com", name="打手甲")
        self._perf(self._order(), user=other_a, bonus_amount=Decimal("10.00"))
        self._perf(self._order(), user=other_a, bonus_amount=Decimal("5.00"))
        self._perf(self._order(), user=other_b, bonus_amount=Decimal("7.00"))
        self._perf(self._order(), user=self.taker, bonus_amount=Decimal("25.00"))

        rows = self._payment_rows(self._workbook())

        self.assertEqual([row[0] for row in rows], [1, 2])
        self.assertEqual(
            [row[1] for row in rows], ["shared@example.com", "taker@example.com"]
        )

    def test_one_account_with_two_different_names_takes_the_sorted_first(self):
        """spec §3.2：同一账号出现两个不同 `alipay_name` ⇒ 取**名称排序后的第一条**
    （与行序无关，输出可复现）。

    ⚠️ 夹具**故意让「先出现的那条」= 李四**（用户名升序下 `xlsx-pay-name` 在 `xlsx-taker`
    之前，而名称序下「张三」在「李四」之前）⇒ 这条能分辨「取排序第一条」与
    「取先出现的那条」两种实现。
"""
        other = User.objects.create_user(username="xlsx-pay-name")
        self._profile(other, account="same@example.com", name="李四")
        self._profile(self.taker, account="same@example.com", name="张三")
        self._perf(self._order(), user=other, bonus_amount=Decimal("25.00"))
        self._perf(self._order(), user=self.taker, bonus_amount=Decimal("25.00"))

        rows = self._payment_rows(self._workbook())

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "张三")

    def test_amount_is_the_sum_of_the_bonus_amounts_of_the_account(self):
        """判据 ⑦：金额 = 该账号下所有行的 `bonus_amount` 之和（两个已知数相加）。"""
        other = User.objects.create_user(username="xlsx-pay-sum")
        self._profile(self.taker, account="sum@example.com", name="打手甲")
        self._profile(other, account="sum@example.com", name="打手甲")
        self._perf(self._order(), user=self.taker, bonus_amount=Decimal("12.50"))
        self._perf(self._order(), user=other, bonus_amount=Decimal("30.25"))

        rows = self._payment_rows(self._workbook())

        self.assertEqual(len(rows), 1)
        self.assertEqual(Decimal(str(rows[0][3])), Decimal("42.75"))

    def test_the_amount_cell_is_numeric_and_shows_two_decimals(self):
        """判据 ⑧：金额是**数值格**（`data_type == 'n'`）、⛔ 不是文本格，且两位小数。"""
        self._profile(self.taker, account="money@example.com", name="打手甲")
        self._perf(self._order(), user=self.taker, bonus_amount=Decimal("55.00"))

        cell = self._workbook()[PAYMENT_SHEET].cell(row=3, column=4)

        self.assertEqual(cell.data_type, "n")
        self.assertNotIsInstance(cell.value, str)
        self.assertEqual(Decimal(str(cell.value)), Decimal("55.00"))
        self.assertEqual(cell.number_format, "0.00")

    def test_account_and_name_are_taken_from_the_staff_profile(self):
        """判据 ⑨：`收款方支付宝账号` / `收款方姓名` 取自 `StaffProfile`（逐字）。"""
        self._profile(self.taker, account="zhangsan@example.com", name="张三")
        self._perf(self._order(), user=self.taker, bonus_amount=Decimal("25.00"))

        row = self._payment_rows(self._workbook())[0]

        self.assertEqual(row[1], "zhangsan@example.com")
        self.assertEqual(row[2], "张三")
        self._assert_blank(row[4])            # 备注：选填，本轮不臆造内容

    def test_rows_of_both_settle_statuses_join_the_same_account(self):
        """判据 ⑩：范围 = **当前筛选结果**，⛔ 不按结账状态过滤——
    已结账与未结账两行都在同一结果里 ⇒ **两行都参与合计**。"""
        self._profile(self.taker, account="both@example.com", name="打手甲")
        self._perf(
            self._order(),
            user=self.taker,
            bonus_amount=Decimal("30.00"),
            settle_status=SettleStatus.SETTLED,
        )
        self._perf(
            self._order(),
            user=self.taker,
            bonus_amount=Decimal("12.00"),
            settle_status=SettleStatus.UNSETTLED,
        )

        rows = self._payment_rows(self._workbook())

        self.assertEqual(len(rows), 1)
        self.assertEqual(Decimal(str(rows[0][3])), Decimal("42.00"))


class PaymentSkipListTests(_ExportFixture):
    """需求 B 的第 4 张表（打款跳过名单）：谁没被打款、为什么——正反两面都断言。"""

    def test_skipped_people_are_listed_with_their_reason_and_kept_out_of_the_payment_sheet(self):
        """判据 ⑪：账号空 / 姓名空 / 合计不大于 0 各一条 ⇒ **出现在跳过名单**、
    且**不在打款表**（正反两面）。"""
        no_account = User.objects.create_user(username="xlsx-no-account")
        no_name = User.objects.create_user(username="xlsx-no-name")
        zero = User.objects.create_user(username="xlsx-zero")
        paid = User.objects.create_user(username="xlsx-paid")
        self._profile(no_account, account="", name="有姓名")
        self._profile(no_name, account="noname@example.com", name="")
        self._profile(zero, account="zero@example.com", name="零金额")
        self._profile(paid, account="paid@example.com", name="已打款")
        self._perf(self._order(), user=no_account, bonus_amount=Decimal("10.00"))
        self._perf(self._order(), user=no_name, bonus_amount=Decimal("11.00"))
        self._perf(self._order(), user=zero, bonus_amount=Decimal("12.00"))
        self._perf(self._order(), user=zero, bonus_amount=Decimal("-12.00"))
        self._perf(self._order(), user=paid, bonus_amount=Decimal("13.00"))

        workbook = self._workbook()
        sheet = workbook[SKIPPED_SHEET]
        self.assertEqual(
            [sheet.cell(row=1, column=column).value for column in range(1, 6)],
            EXPECTED_SKIPPED_HEADERS,
        )
        skipped = {(row[0], row[3]): row for row in self._skipped_rows(workbook)}

        self.assertIn(("xlsx-no-account", "没填结算支付宝账号"), skipped)
        self.assertIn(("xlsx-no-name", "没填结算支付宝姓名"), skipped)
        self.assertIn(("xlsx-zero", "合计金额不大于 0"), skipped)

        # 反面：这三个人都**不在**打款表里（表里没有用户名列 ⇒ 按他们的账号判）。
        accounts = [row[1] for row in self._payment_rows(workbook)]
        self.assertEqual(accounts, ["paid@example.com"])
        for account in ("", "noname@example.com", "zero@example.com"):
            self.assertNotIn(account, accounts)

        # 未打款金额 = 这个人自己那一份（数值格、两位小数）。
        zero_row = skipped[("xlsx-zero", "合计金额不大于 0")]
        self.assertEqual(zero_row[1], "zero@example.com")
        self.assertEqual(Decimal(str(zero_row[4])), Decimal("0.00"))
        self.assertEqual(
            Decimal(str(skipped[("xlsx-no-account", "没填结算支付宝账号")][4])),
            Decimal("10.00"),
        )

    def test_a_person_without_a_profile_row_is_skipped_and_never_paid(self):
        """**没有 `StaffProfile` 行**是**独立的一条路**（与「有行但没填账号」不同：
    前者 `profiles.get(username) is None`，后者拿到一行但值为空串）。

    判据：他进跳过名单（账号与姓名**各一条原因**，一行一因，两条都断言）、
    **不在打款表**；⚠️ 并配**反向对照**（同批里放一个有档案的人）⇒ 排除「打款表整体空了」
    这种假通过。
"""
        nobody = User.objects.create_user(username="xlsx-no-profile")
        paid = User.objects.create_user(username="xlsx-with-profile")
        self._profile(paid, account="paid@example.com", name="已打款")
        self._perf(self._order(), user=nobody, bonus_amount=Decimal("9.00"))
        self._perf(self._order(), user=paid, bonus_amount=Decimal("13.00"))

        workbook = self._workbook()
        rows = [
            row
            for row in self._skipped_rows(workbook)
            if row[0] == "xlsx-no-profile"
        ]

        self.assertEqual(
            [row[3] for row in rows], ["没填结算支付宝账号", "没填结算支付宝姓名"]
        )
        # 没有档案行 ⇒ 两行的账号与姓名都逐字取不到（空格，⛔ 不编造）。
        for row in rows:
            self._assert_blank(row[1])
            self._assert_blank(row[2])
            self.assertEqual(Decimal(str(row[4])), Decimal("9.00"))
        # 反面：打款表里**只有**那个有档案的人（他的账号，不是别人的）。
        self.assertEqual(
            [row[1] for row in self._payment_rows(workbook)], ["paid@example.com"]
        )

    def test_a_blank_name_with_a_non_positive_total_gets_both_reasons(self):
        """「一行一因」的独立性：**没填姓名**与**本人合计 ≤ 0**是两条因 ⇒ 两行都要有。

    ⚠️ 这个人**没姓名 ⇒ 不进账号分组**，所以「≤ 0」这条**不能只挂在分组上**——
    否则他只拿到一条（评审 P2-1 实测到的正是这个漏行）。
"""
        user = User.objects.create_user(username="xlsx-blank-name-zero")
        self._profile(user, account="blank@example.com", name="")
        self._perf(self._order(), user=user, bonus_amount=Decimal("0.00"))

        rows = self._skipped_rows(self._workbook())
        rows = [row for row in rows if row[0] == "xlsx-blank-name-zero"]

        self.assertEqual(
            [row[3] for row in rows],
            ["没填结算支付宝姓名", "合计金额不大于 0"],       # 顺序 = 既有原因序
        )

    def test_a_person_without_a_profile_row_and_a_non_positive_total_gets_exactly_three(self):
        """一个人**最多三行**：没档案 ⇒ 账号空 + 姓名空，再加上本人合计 ≤ 0 ⇒ **恰好三条**。

    ⛔ 用**恰好集合**断言（不是 `assertIn`）：多一行 / 少一行都红。
"""
        nobody = User.objects.create_user(username="xlsx-no-profile-zero")
        self._perf(self._order(), user=nobody, bonus_amount=Decimal("0.00"))

        rows = self._skipped_rows(self._workbook())
        rows = [row for row in rows if row[0] == "xlsx-no-profile-zero"]

        self.assertEqual(len(rows), 3)
        self.assertEqual(
            {row[3] for row in rows},
            {"没填结算支付宝账号", "没填结算支付宝姓名", "合计金额不大于 0"},
        )
        self.assertEqual(
            [row[3] for row in rows],
            [
                "没填结算支付宝账号",
                "没填结算支付宝姓名",
                "合计金额不大于 0",
            ],
        )

    def test_a_positive_member_of_a_non_positive_account_gets_only_the_group_reason(self):
        """账号分组合计 ≤ 0 时，**成员本人合计为正**的人也拿「合计金额不大于 0」——那是
    **分组**那条（「整个账号打不出去」，防静默漏人）；⛔ 不能再多出一条「本人 ≤ 0」。

    同批另一个成员**本人合计 ≤ 0** ⇒ 两个触发面都命中，但**同原因只出一行**（去重）。
"""
        positive = User.objects.create_user(username="xlsx-grp-a")
        negative = User.objects.create_user(username="xlsx-grp-b")
        self._profile(positive, account="grp@example.com", name="甲")
        self._profile(negative, account="grp@example.com", name="甲")
        self._perf(self._order(), user=positive, bonus_amount=Decimal("5.00"))
        self._perf(self._order(), user=negative, bonus_amount=Decimal("-5.00"))

        workbook = self._workbook()
        rows = self._skipped_rows(workbook)
        positive_rows = [row for row in rows if row[0] == "xlsx-grp-a"]
        negative_rows = [row for row in rows if row[0] == "xlsx-grp-b"]

        # 本人合计 +5.00 ⇒ 只有**分组**那条（恰好一行）。
        self.assertEqual(len(positive_rows), 1)
        self.assertEqual([row[3] for row in positive_rows], ["合计金额不大于 0"])
        # 本人合计 -5.00 ⇒ 「本人 ≤ 0」与「分组 ≤ 0」同原因 ⇒ 去重成一行。
        self.assertEqual(len(negative_rows), 1)
        self.assertEqual([row[3] for row in negative_rows], ["合计金额不大于 0"])
        # 整个账号打不出去 ⇒ 打款表里没有这个账号。
        self.assertEqual(self._payment_rows(workbook), [])


class OrderMoneyColumnsTests(_ExportFixture):
    """⭐ 2026-10-22（业务方拍板「导出同步遮」）：不持可看判据时，订单金额族**整列不出现**。

    判据与列表/手机页**同一处实现**（`performance_views.can_view_order_money`，两节点任一：
    `order.list` / `finance.payroll`）。不持 ⇒ 明细「订单价格 / 退款」与汇总
    「总计订单价 / 总计退款」从**表头与每一行**里一并删掉（明细 29→27、汇总 10→8）；
    ⛔ 不是填 0 / 空串。

    ⚠️ 三个用例互为对照：不持（只有导出节点）⇒ 没有这两列；持 `order.list` ⇒ 有；
    只持 `finance.payroll`（业务方补的那把钥匙）⇒ 也有。
    ⚠️ 每条都以**表头集合**（字面列表）与**数据行的格数/值**双判据打，⛔ 不只断言「文件能生成」。
"""

    #: 期望表头 = 附件 29 / 10 列减去被遮的四项（用测试文件自己的字面副本推导，⛔ 不引被测模块常量）。
    _MASKED_DETAIL = ("订单价格", "退款")
    _MASKED_SUMMARY = ("总计订单价", "总计退款")

    def _one_row_for(self, client, username):
        user = User.objects.get(username=username)
        order = self._order(total_price=Decimal("268.00"), refund_fee=Decimal("8.00"))
        self._perf(order, user=user)
        return client

    def test_a_custom_role_with_only_the_export_node_gets_no_order_money_columns(self):
        # 只持导出节点 133、不持 14 / 80 ⇒ 数据范围收窄成「只看自己的行」（那也是本用例要导的）。
        client = self._client("xlsx-mask-own", NODE_EXPORT)
        self._one_row_for(client, "xlsx-mask-own")

        workbook = self._workbook(client)
        detail = self._detail_rows(workbook)
        summary = self._summary_rows(workbook)

        expected_detail = [h for h in EXPECTED_DETAIL_HEADERS if h not in self._MASKED_DETAIL]
        expected_summary = [h for h in EXPECTED_SUMMARY_HEADERS if h not in self._MASKED_SUMMARY]
        self.assertEqual(detail[0], expected_detail)
        self.assertEqual(summary[0], expected_summary)
        for header in self._MASKED_DETAIL + self._MASKED_SUMMARY:
            self.assertNotIn(header, detail[0] + summary[0])

        row = detail[1]
        self.assertEqual(len(row), 27)
        # 对齐：删掉两列后下标 3 变成「提成类型」（原来是「订单价格」）。
        self.assertEqual(row[3], EmpType.MAIN)
        # 值不出现（268 = 订单价、8 = 退款；`Decimal("268") == 268`，两种读回形状都覆盖）。
        self.assertNotIn(268, row)
        self.assertNotIn(8, row)
        self._assert_money(row[1], "25.00")         # 预计额照旧
        self._assert_money(row[2], "25.00")         # 实计额照旧

        summary_row = summary[1]
        self.assertEqual(len(summary_row), 8)
        self.assertNotIn(268, summary_row)
        self.assertNotIn(8, summary_row)

    def test_a_manager_with_order_list_gets_the_two_columns_back(self):
        client = self._client("xlsx-mask-mgr", NODE_EXPORT, "order.list")
        self._perf(
            self._order(total_price=Decimal("268.00"), refund_fee=Decimal("8.00")),
            user=self.taker,
        )

        workbook = self._workbook(client)

        self.assertEqual(self._detail_rows(workbook)[0], EXPECTED_DETAIL_HEADERS)
        self.assertEqual(self._summary_rows(workbook)[0], EXPECTED_SUMMARY_HEADERS)
        row = self._detail_rows(workbook)[1]
        self.assertEqual(len(row), 29)
        self._assert_money(row[3], "268.00")         # 订单价格
        self._assert_money(row[4], "8.00")           # 退款

    def test_a_finance_payroll_only_role_gets_the_two_columns_back(self):
        """业务方 2026-10-22：财务（只持 `finance.payroll`）也看得到——与列表同一判据。"""
        client = self._client("xlsx-mask-fin", NODE_EXPORT, "finance.payroll")
        self._one_row_for(client, "xlsx-mask-fin")

        workbook = self._workbook(client)

        self.assertEqual(self._detail_rows(workbook)[0], EXPECTED_DETAIL_HEADERS)
        row = self._detail_rows(workbook)[1]
        self.assertEqual(len(row), 29)
        self._assert_money(row[3], "268.00")
        self._assert_money(row[4], "8.00")


class OfferPriceRedactionTests(_ExportFixture):
    """`发单价` 的脱敏分支：`order.restrict_view_offer_price` 被施加 ⇒ 空白。"""

    def test_offer_price_is_blank_only_when_the_flag_is_imposed(self):
        order = self._order(hall_price=Decimal("120.00"))
        self._perf(order, user=self.taker)

        # 两个 client 都要 `order.list`（管理侧数据范围）：只有导出节点时，
        # `performance_queryset` 会把数据收窄成「只看自己」，看不到 `self.taker` 的行。
        open_client = self._client("xlsx-open", NODE_EXPORT, "order.list")
        restricted = self._client(
            "xlsx-hidden", NODE_EXPORT, "order.list", NODE_RESTRICT_OFFER_PRICE
        )

        visible = self._detail_rows(self._workbook(open_client))[1]
        hidden = self._detail_rows(self._workbook(restricted))[1]

        self._assert_money(visible[8], "120.00")     # 非管理员、无该 flag ⇒ 照常出数
        self._assert_blank(hidden[8])                # 该 flag 生效 ⇒ 留空


class OverTheRowCapTests(_ExportFixture):
    """超上限**报错不截断**：仍走统一信封 + HTTP 200（brief §5.1）。"""

    def test_over_the_row_cap_returns_the_envelope_not_a_partial_file(self):
        self._perf(self._order(), user=self.taker)
        self._perf(self._order(), user=self.taker)

        # 真造两行、把上限压到 1（上限本身在 `writes` 里已有一条**真到 10000 行**的用例守着）。
        with mock.patch("apps.business.api.performance_write_views.EXPORT_MAX_ROWS", 1):
            response = self.finance.get(EXPORT_URL)

        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 1000, response.json())
        # ⚠️ 判据钉**两个具体数字**（真实行数 `2` 与被压到 `1` 的上限），不用 `assertIn("1", …)`
        # ——后者在 `msg` 含任何数字 1 时都真（近似恒真，E-0065 同族）。
        msg = response.json()["msg"]
        self.assertIn("本次导出 2 行", msg)
        self.assertIn("超过上限 1 行", msg)
        self.assertTrue(response["Content-Type"].startswith("application/json"))
        self.assertNotEqual(response["Content-Type"], XLSX_MIME)   # ⛔ 没有半截文件


class FilenameHeaderTests(_ExportFixture):
    """响应头：ASCII 兜底名 + RFC 5987 `filename*`（中文名只有后者能被浏览器可靠采用）。"""

    def test_response_offers_both_ascii_and_rfc5987_filenames(self):
        self._perf(self._order(), user=self.taker)

        response = self.finance.get(EXPORT_URL)

        self.assertEqual(response["Content-Type"], XLSX_MIME)
        disposition = response["Content-Disposition"]
        today = timezone.localdate().strftime("%Y%m%d")
        self.assertIn(f'filename="performances-{today}.xlsx"', disposition)
        extended = re.search(r"filename\*=UTF-8''([^;]+)", disposition)
        self.assertIsNotNone(extended, disposition)
        self.assertRegex(
            unquote(extended.group(1)), r"^效绩提成记录\d{10}\.xlsx$"
        )


class SafeCellTextUnitTests(SimpleTestCase):
    """D-0334（二轮 Z13 / 五轮 M-08：xlsx 公式注入）—— `safe_cell_text` 的函数层判据。

    业务方拍板口径：**导出边界转义**——写单元格前，文本若以
    `=` / `+` / `-` / `@`（表格公式起始符）开头 ⇒ 前缀 `'`；⛔ 数字 / 日期 /
    普通中文完全原样、⛔ 不动导入、⛔ 不动屏幕显示。
    助手与 `safe_cell_value`（E-0242 的净化）**串联**：先剔 XML 非法控制字符，
    再对净化后的结果判起始符（前缀打在净化结果上，`\x0b=` 净化后仍要转义）。
"""

    def test_four_formula_leading_prefixes_are_escaped(self):
        for text in ("=SUM(A1)", "+甲", "-乙", "@丙"):
            with self.subTest(text=text):
                self.assertEqual(safe_cell_text(text), "'" + text)

    def test_lone_prefix_character_is_escaped_too(self):
        """孤字符也转义：规则按「起始符」一刀切，⛔ 不设 `len > 1` 例外
    （openpyxl 的公式判定有 `len > 1`，但转义口径跟业务拍板走，不做第二套判据）。"""
        self.assertEqual(safe_cell_text("="), "'=")

    def test_empty_string_stays_empty(self):
        self.assertEqual(safe_cell_text(""), "")

    def test_normal_text_without_prefix_is_kept_verbatim(self):
        self.assertEqual(safe_cell_text("原神代练（双排）"), "原神代练（双排）")

    def test_non_string_values_pass_through_untouched(self):
        """⭐ 回归钉：负数金额是 `Decimal`（数值格），⛔ 不许被当文本转义；`None` / `int` 同理。"""
        negative = Decimal("-5.00")
        self.assertIs(safe_cell_text(negative), negative)
        self.assertIsNone(safe_cell_text(None))
        self.assertEqual(safe_cell_text(-7), -7)

    def test_illegal_characters_are_still_stripped_before_the_escape(self):
        """串联次序钉死：先净化（剔 `\\x0b`）再判起始符 ⇒ 前缀打在净化后的结果上。"""
        self.assertEqual(safe_cell_text("\x0b=甲"), "'=甲")
        self.assertEqual(safe_cell_text("甲\x0b乙"), "甲乙")


class FormulaEscapeTests(_ExportFixture):
    """D-0334 端到端：以公式起始符开头的文本进导出 ⇒ **文本格**（`data_type == 's'`、
    读回带 `'` 前缀）；数值格（含负数）与普通文本零变化。

    修前形状（openpyxl 3.1.5 `Cell._bind_value` 读源码 + 探针实锤）：`len > 1` 且以 `=`
    开头的字符串写格即被绑成公式 ⇒ `data_only=True` 读回 `None`——文本**静默丢失**，
    Excel 打开还会求值。判据打在真实出口（HTTP 导出字节读回）。
"""

    def test_formula_leading_project_text_is_exported_as_text(self):
        """`项目` 以 `=` 开头 ⇒ 格 = `'=原文` 且 `data_type == 's'`（修前这条红）。"""
        order = self._order(project_text="=公式项目甲")
        self._perf(order, user=self.taker)

        cell = self._workbook()["绩效明细"].cell(row=2, column=14)   # 第 14 列「项目」

        self.assertEqual(cell.data_type, "s")
        self.assertEqual(cell.value, "'=公式项目甲")

    def test_numeric_cells_including_negative_refund_stay_numeric(self):
        """回归钉（零行为意外）：金额格照旧**数值**（⛔ 不许把 -2.50 写成 "'-2.50"）、
    普通中文文本逐字不变。⚠️ `refund_fee = -2.50` 是故意的负数边界（D-0015）。"""
        order = self._order(project_text="正常项目乙", refund_fee=Decimal("-2.50"))
        self._perf(order, user=self.taker)

        sheet = self._workbook()["绩效明细"]
        bonus_cell = sheet.cell(row=2, column=3)     # 第 3 列「实计额」
        refund_cell = sheet.cell(row=2, column=5)    # 第 5 列「退款」（本例为负数）
        project_cell = sheet.cell(row=2, column=14)  # 第 14 列「项目」（普通文本）

        self.assertEqual(bonus_cell.data_type, "n")
        self.assertEqual(Decimal(str(bonus_cell.value)), Decimal("25.00"))
        self.assertEqual(refund_cell.data_type, "n")
        self.assertEqual(Decimal(str(refund_cell.value)), Decimal("-2.50"))
        self.assertEqual(project_cell.value, "正常项目乙")

    def test_formula_leading_alipay_account_is_escaped_in_the_payment_sheet(self):
        """打款表的收款账号（跨用户自由文本，Z13 点名的面）以 `=` 开头 ⇒ 同样转义。"""
        self._profile(self.taker, account="=target@example.com", name="收款甲")
        self._perf(self._order(), user=self.taker, bonus_amount=Decimal("25.00"))

        cell = self._workbook()[PAYMENT_SHEET].cell(row=3, column=2)

        self.assertEqual(cell.data_type, "s")
        self.assertEqual(cell.value, "'=target@example.com")
