"""订单导出端点（`GET /api/business/orders/export`，spec §2 / §8-1；实施计划 Task 3）。

三条纪律（全仓同）：
- 期望值写**字面值**（E-0024）：节点 code、错误码、上限 10000、响应头里的文件名形状都写字面；
- 断言打在**真实出口**：读 HTTP 响应体（xlsx 字节用 `openpyxl` 读回；信封读 JSON）；
- 夹具**一律合成值**（E-0077）。

⚠️ **执行期对计划骨架的 8 处最小修正**（计划里的代码按「即将入库」写，但逐字跑不通，
当场按**依赖源码**改正；判据一个字没放宽、也没换）：
1. `make_logged_in_client(groups=…)` **必须给**：`_grant_nodes` 只把节点挂到
   `user.groups → Role` 上（`tests/p5_auth_helpers.py:198-206`），只传 `nodes=` 而
   `groups=()`（默认）⇒ 节点**一个都挂不上**。已用一次性探针实测：
   `NODES_WITHOUT_GROUPS = []` / `NODES_WITH_GROUPS = ['order.export']`。
2. `restrict_to_self_created` 那条用**独立用户名**：计划骨架复用 `测试导出甲`（已在 `setUp`
   建过），而夹具走 `create_user` ⇒ 撞 `auth_user` 的唯一约束。
3. `soft_delete_order(actor=…)` 收的是**User 对象**（`recycle.py:85` 调 `actor.get_username()`），
   计划骨架传的是字符串 ⇒ `AttributeError`。
4. `GameServer.objects.create(…)` **必须给 `game`**（`apps/business/models.py:58-60` 是非空 FK），
   计划骨架漏了 ⇒ `IntegrityError: null value in column "game_id"`。
5. 列表腿要**另持 `order.list`**：`OrderViewSet` 的读路径按 `order.list` 放行
   ⇒ 只持 `order.export` 的 client 拿列表会 **403 + `data` 为 `None`**（B6/B10① 的两路对拍需要它）。
6. 列表分页载荷的键是 **`items`** 不是 `results`（`core/envelope.py:25-30` 的 `paginated()`）。
7. `test_permission_is_enforced` 的「无权限」client 必须用**另一个组**：节点挂在
   `Group → Role` 上（`Role.name` 唯一），两个 client 同组 ⇒ 权限**取并集**、
   无权限那个也会拿到 `order.export`（实测：denied 得 200 而非 403）。
8. 「空值 ⇒ 空串」的**端点腿**不能用 `cell.value == ""` 判：openpyxl 读回时把
   空串与「没写」**都变成 `None`** ⇒ 见 `_written_cells` 与 `_assert_empty_string_cell`
   。
"""
import re
import zipfile
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from unittest import mock

from django.contrib.auth.models import User
from django.urls import resolve
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from openpyxl.utils import get_column_letter

from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType, Game, GameServer, Order, OrderRefund, OrderSource, OrderSourceGroup,
    OrderStatus, Performance,
)
from apps.business.recycle import soft_delete_order
from tests.p5_auth_helpers import make_logged_in_client
from tests.xlsx_helpers import workbook_of

EXPORT_URL = "/api/business/orders/export"
LIST_URL = "/api/business/orders/"
NODE_EXPORT = "order.export"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: 夹具用组名（`Role.name` 的 `max_length=5` ⇒ 组名必须 ≤5 字；「客服」是内置五职位之一）。
TEST_GROUP = "客服"


#: 单元格开标签：捕获**坐标**与该标签自己的**其余属性**（`t=` 就在这里面）。
_CELL_TAG = re.compile(r'<c r="([A-Z]+\d+)"([^>]*)>')
#: 属性串里的类型标记（`inlineStr` = 空串；`n` = 数值 / 写 `None` 的带样式格）。
_CELL_TYPE = re.compile(r't="([^"]+)"')


def _written_cells(response):
    """响应字节里 `Sheet1` **真的写出来**的单元格：`{坐标: XML 类型}`（无 `t=` 则为 `None`）。

    ⚠️ 为什么不能只看 `cell.value`：openpyxl 读回时把**空串**与 `None` 都变成 `None`。
    ⚠️ 为什么不能只看「坐标在不在」：本表**每个数据格都设了样式**
    （`order_export.build_workbook` 里 `cell.alignment = centered`），而带样式的格子即使写
    `None` 也会写出 `<c r="…" s="1" t="n" />`——**不是**整格省略 ⇒ 只取坐标分辨不出两者。
    2026-09-27 用本仓的 openpyxl 3.1.5 实测的四种形状（最小复现）：
    无样式 + `None` ⇒ 该 `<c>` 整个省略；无样式 + `''` ⇒ `<c … t="inlineStr" />`；
    有样式 + `None` ⇒ `<c … s="1" t="n" />`；有样式 + `''` ⇒ `<c … s="1" t="inlineStr" />`。
    ⇒ 能分辨「空串」与「写 None」的只有 **`t=` 属性**（`inlineStr` ⇒ 写的是空串）。
    """
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode()
    cells = {}
    for tag in _CELL_TAG.finditer(sheet_xml):
        found = _CELL_TYPE.search(tag.group(2))
        cells[tag.group(1)] = found.group(1) if found else None
    return cells


class OrderExportApiTests(TenantTestCase):
    def setUp(self):
        self.game_a = Game.objects.create(name="测试游戏甲")
        self.game_b = Game.objects.create(name="测试游戏乙")
        # ⚠️ `GameServer.game` 是**非空** FK（`apps/business/models.py:58-60`）⇒ 必须给游戏；
        #    计划骨架漏了它 ⇒ `IntegrityError: null value in column "game_id"`（见模块 docstring）。
        self.server = GameServer.objects.create(game=self.game_a, name="测试区服甲")
        self.client_a = make_logged_in_client(
            username="测试导出甲", groups=(TEST_GROUP,),
            # ⚠️ 另持 `order.list`：B6/B10① 要拿**列表端点**与导出对拍同一集合，而列表读路径
            #    按 `order.list` 放行（只持 `order.export` 会 403 + `data=None`）。
            nodes=(NODE_EXPORT, "order.list"), games=[self.game_a],
        )
        self.client_b = make_logged_in_client(
            username="测试导出乙", groups=(TEST_GROUP,), nodes=(NODE_EXPORT,),
            games=[self.game_b],
        )

    def _order(self, no, *, game, **overrides):
        base = dict(
            order_no=no, game=game, server=self.server,
            status=OrderStatus.IN_PROGRESS, content="内容",
            total_price=Decimal("10.00"), refund_fee=Decimal("0.00"),
            hall_price=Decimal("8.00"),
        )
        base.update(overrides)
        return Order.objects.create(**base)

    def _data_rows(self, response):
        return [
            [cell.value for cell in row]
            for row in workbook_of(response)["Sheet1"].iter_rows(min_row=2)
        ]

    def _assert_empty_string_cell(self, response, order_no, column_index):
        """该单的 `column_index`（0-based）那一格**必须写成空串**。

        三条合起来才是 spec §3 开头那条口径：① 单元格**存在**（不是被整格省略的 `None`）；
        ② 该格的 XML 类型是 **`inlineStr`**（写 `''` 的唯一形状；写 `None` 是 `t="n"` 或整格省略）；
        ③ 读回为空。单看 `cell.value` 区分不出「空串」与「没写」（见 `_written_cells`）。
        """
        rows = self._data_rows(response)
        row_index = [row[1] for row in rows].index(order_no)
        coordinate = f"{get_column_letter(column_index + 1)}{row_index + 2}"
        cells = _written_cells(response)
        self.assertIn(
            coordinate,
            cells,
            "该格被整格省略了（写的是 None / 空单元格，spec §3 要求空串）",
        )
        self.assertEqual(
            cells[coordinate],
            "inlineStr",
            f"{coordinate} 的 XML 类型不是 inlineStr（写 None 会是 t=\"n\"）⇒ 不是空串",
        )
        self.assertIn(rows[row_index][column_index], ("", None))

    def test_route_is_literal_without_a_trailing_slash(self):            # B16
        match = resolve(EXPORT_URL)
        self.assertEqual(match.url_name, "order-export")
        self.assertEqual(match.route, "api/business/orders/export")

    def test_headers_and_single_sheet(self):                             # B1（端点腿）
        self._order("20260927000000000011", game=self.game_a)
        response = self.client_a.get(EXPORT_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], XLSX_MIME)
        self.assertEqual(workbook_of(response).sheetnames, ["Sheet1"])

    def test_unchecked_mode_exports_exactly_the_same_set_as_the_list(self):   # B6 + B10①
        nos = ["20260927000000000021", "20260927000000000022"]
        for no in nos:
            self._order(no, game=self.game_a)
        # ⭐ 另有一张**筛掉**的单（状态不同）：没有它，「导出丢掉筛选条件」这条变异
        #    在这条用例上**不会红**（可见集合与筛选结果恰好相同）⇒ 判据杀不了东西。
        self._order(
            "20260927000000000023", game=self.game_a, status=OrderStatus.COMPLETED
        )
        listed = self.client_a.get(LIST_URL + "?status=IN_PROGRESS").json()
        list_nos = sorted(row["order_no"] for row in listed["data"]["items"])
        rows = self._data_rows(self.client_a.get(EXPORT_URL + "?status=IN_PROGRESS"))
        self.assertEqual(sorted(row[1] for row in rows), list_nos)
        self.assertEqual(list_nos, sorted(nos))

    def test_status_multiselect_exports_the_same_rows_as_the_list(self):
        """⭐ 2026-10-29 实装多选：**列表多选 ⇒ 导出同结果**（两个入口共用同一个
        `order_search_params` + `search_orders`，判据钉住「多值的并集语义两边一致」）。

        判别力：导出侧若漏掉多值语义（比如仍按单值等值筛），第一半红；夹具里另放一张
        **只命中一个码**的单与一张**两个码都不命中**的单，防「两边恰好都空 / 都全量」的假绿。
        """
        hit_in_progress = self._order("20260927000000000031", game=self.game_a)
        hit_unassigned = self._order(
            "20260927000000000032", game=self.game_a, status=OrderStatus.UNASSIGNED
        )
        self._order("20260927000000000033", game=self.game_a, status=OrderStatus.COMPLETED)
        query = f"?status={OrderStatus.IN_PROGRESS},{OrderStatus.UNASSIGNED}"
        listed = self.client_a.get(LIST_URL + query).json()
        self.assertEqual(listed["code"], 0, listed)
        list_nos = sorted(row["order_no"] for row in listed["data"]["items"])
        rows = self._data_rows(self.client_a.get(EXPORT_URL + query))
        exported = sorted(row[1] for row in rows)
        self.assertEqual(exported, list_nos)
        # 非空哨兵：多值的**并集**真的生效（两个码各命中一张、第三张不出现）
        self.assertEqual(list_nos, sorted([hit_in_progress.order_no, hit_unassigned.order_no]))

    def test_hall_publisher_filter_exports_the_same_rows_as_the_list(self):
        """⭐ 2026-10-26「发单人筛选」线 §3-⑤：带 `hall_publisher_user` 时导出与列表**同源**。

        判别力：导出视图（`order_export_views.py`）漏转发 `hall_publisher_user` ⇒
        导出会多出「别人抛的单」与「没有发单人的单」⇒ 第一半红。夹具里另放两张
        **应被筛掉**的单（另一个发单人 / 没有发单人），否则「条件被丢掉」也照样绿。
        """
        publisher_a = User.objects.create_user(username="测试导出人甲", password="x")
        publisher_b = User.objects.create_user(username="测试导出人乙", password="x")
        hit = self._order(
            "20260927000000000051", game=self.game_a, hall_publisher=publisher_a
        )
        self._order("20260927000000000052", game=self.game_a, hall_publisher=publisher_b)
        self._order("20260927000000000053", game=self.game_a)  # 无发单人
        query = f"?hall_publisher_user={publisher_a.pk}"
        listed = self.client_a.get(LIST_URL + query).json()
        list_nos = sorted(row["order_no"] for row in listed["data"]["items"])
        rows = self._data_rows(self.client_a.get(EXPORT_URL + query))
        exported = sorted(row[1] for row in rows)
        self.assertEqual(exported, list_nos)
        # 非空哨兵：条件真的生效，而不是「两边恰好都空」。
        self.assertEqual(list_nos, [hit.order_no])

    def test_split_id_is_forwarded_and_the_export_does_not_fold(self):
        """⭐ 批次 4 线 B（拆单）：`?split_id=` **必须被转发**（⛔ 不许静默丢条件），
        且导出**不折叠**（列表只数主行，导出的每一张真实订单行都在——spec §6 的已登记代价）。

        判别力：把 `order_export_views` 里那一行 `split_id=data.get("split_id")` 删掉 ⇒
        本用例第一半红（导出会带上**别的组**那张单）；把折叠塞进导出 ⇒ 第二半红。
        """
        group = "72000000000000000001"
        in_group = self._order("20260927000000000041", game=self.game_a, split_id=group)
        child = self._order("20260927000000000042", game=self.game_a, split_id=group)
        # ⭐ 反向对照：另一张**不在这一组**的单（没有它，「条件被丢掉」这条变异不会红）。
        outside = self._order("20260927000000000043", game=self.game_a)

        rows = self._data_rows(self.client_a.get(EXPORT_URL + f"?split_id={group}"))
        exported = sorted(row[1] for row in rows)
        # ① 条件真的生效（组外那张不出现）
        self.assertNotIn(outside.order_no, exported)
        # ② **不折叠**：组内两笔都在
        self.assertEqual(exported, sorted([in_group.order_no, child.order_no]))
        # ③ 反向对照（那一半才是「导出不折叠」的判据）：**不带参数**时，同一组在导出里
        #    是两行、在列表里是**一行**（列表的折叠挂在 `filter_queryset`，只对 `list` 生效）
        exported_all = {
            row[1] for row in self._data_rows(self.client_a.get(EXPORT_URL))
        }
        self.assertIn(in_group.order_no, exported_all)
        self.assertIn(child.order_no, exported_all)
        listed_nos = {
            row["order_no"]
            for row in self.client_a.get(LIST_URL).json()["data"]["items"]
        }
        self.assertIn(in_group.order_no, listed_nos)   # 组内 id 最小者 = 列表那一行的代表
        self.assertNotIn(child.order_no, listed_nos)

    def test_checked_mode_exports_exactly_the_given_order_nos(self):     # B6（勾选档）
        keep = self._order("20260927000000000031", game=self.game_a)
        other = self._order("20260927000000000032", game=self.game_a)
        rows = self._data_rows(
            self.client_a.get(EXPORT_URL + f"?order_nos={keep.order_no}")
        )
        self.assertEqual([row[1] for row in rows], [keep.order_no])
        self.assertNotIn(other.order_no, [row[1] for row in rows])

    def test_game_scope_narrows_the_export(self):                        # B10②
        self._order("20260927000000000041", game=self.game_a)
        self._order("20260927000000000042", game=self.game_b)
        rows = self._data_rows(self.client_a.get(EXPORT_URL))
        self.assertEqual([row[1] for row in rows], ["20260927000000000041"])

    def test_restrict_to_self_created_narrows_the_export(self):          # B11
        # ⚠️ 独立用户名（计划骨架复用 setUp 里的 `测试导出甲` ⇒ 撞唯一约束，见模块 docstring）。
        restricted = "测试导出受限"
        mine = self._order("20260927000000000051", game=self.game_a, created_by=restricted)
        self._order("20260927000000000052", game=self.game_a, created_by="测试别人丙")
        client = make_logged_in_client(
            username=restricted, groups=(TEST_GROUP,),
            nodes=(NODE_EXPORT, "order.restrict_to_self_created"),
            games=[self.game_a],
        )
        rows = self._data_rows(client.get(EXPORT_URL))
        self.assertEqual([row[1] for row in rows], [mine.order_no])

    def test_soft_deleted_orders_do_not_appear(self):                    # B12
        gone = self._order("20260927000000000061", game=self.game_a)
        # ⚠️ `actor` 收 **User 对象**（`recycle.py:85` 调 `actor.get_username()`），
        #    计划骨架传字符串会 `AttributeError`，见模块 docstring。
        soft_delete_order(
            order=gone, actor=User.objects.get(username="测试导出甲"), is_del_emp="N"
        )
        rows = self._data_rows(self.client_a.get(EXPORT_URL))
        self.assertEqual(rows, [])

    def test_empty_result_is_a_normal_file_with_only_the_header(self):   # B13
        response = self.client_a.get(EXPORT_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], XLSX_MIME)
        self.assertEqual(workbook_of(response)["Sheet1"].max_row, 1)
        self.assertEqual(workbook_of(response)["Sheet1"].max_column, 31)

    def test_malformed_params_return_the_validation_envelope(self):      # B15
        for query in (
            "?price_max=abc",
            "?status=BOGUS",
            "?order_nos=" + "x" * 129,
            "?order_nos=20260927000000000031,",
            "?batch_lookup_nos=20260927000000000031,",
        ):
            body = self.client_a.get(EXPORT_URL + query).json()
            self.assertEqual(body["code"], 1000, query)

    def test_blank_params_are_same_source_in_both_entries(self):         # B18 导出腿
        self.assertEqual(self.client_a.get(EXPORT_URL + "?status=").status_code, 200)
        self.assertEqual(self.client_a.get(EXPORT_URL + "?game=").status_code, 200)
        body = self.client_a.get(EXPORT_URL + "?payment_status=").json()
        self.assertEqual(body["code"], 1000)

    def test_checked_mode_silently_skips_invisible_order_nos(self):      # B19
        # ⚠️ 判据形状逐字按 spec §8-1 B19：**3 个单号（2 可见 + 1 不可见）⇒ 只 2 行**。
        #    可见的只有 1 个时，「勾选档只取 `split(",")` 的第一个单号」这条变异**杀不掉**
                #    （取第一个与全取等价；实测该变异 SURVIVED）⇒ 必须两个可见单号。
        first_visible = self._order("20260927000000000071", game=self.game_a)
        invisible = self._order("20260927000000000072", game=self.game_b)
        second_visible = self._order("20260927000000000073", game=self.game_a)
        query = (
            f"?order_nos={first_visible.order_no},"
            f"{invisible.order_no},{second_visible.order_no}"
        )
        response = self.client_a.get(EXPORT_URL + query)
        self.assertEqual(response.status_code, 200)
        rows = self._data_rows(response)
        # 行序 = `Order.Meta.ordering`（`apps/business/models.py:541` 的 `["-created_at", "-id"]`）⇒ 后建的在前
        # （两个 `created_at` 即使同微秒，`-id` 也让次序确定）。
        self.assertEqual(
            [row[1] for row in rows],
            [second_visible.order_no, first_visible.order_no],
        )

    def test_permission_is_enforced(self):                               # B9
        self._order("20260927000000000081", game=self.game_a)
        allowed = make_logged_in_client(
            username="测试导出有权限", groups=(TEST_GROUP,), nodes=(NODE_EXPORT,),
            games=[self.game_a])
        denied = make_logged_in_client(
            # ⚠️ **另一个组**：节点挂在 `Group → Role`（`Role.name` 唯一）上，同组会**取并集**
            #    ⇒ 与 `allowed` 同用「客服」时会把 `order.export` 也带给 denied（实测 200）。
            username="测试导出无权限", groups=("客服主管",), nodes=("order.list",),
            games=[self.game_a])
        self.assertEqual(allowed.get(EXPORT_URL).status_code, 200)
        response = denied.get(EXPORT_URL)
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["code"], 403)
        self.assertEqual(body["msg"], "你没有导出订单的权限")
        self.assertIsNone(body["data"])

    def test_claimed_and_refunded_columns_come_from_the_annotations(self):   # 第 19 / 21 列
        order = self._order("20260927000000000091", game=self.game_a)
        taker = User.objects.create_user(username="测试打手乙", password="x")
        claimed = timezone.now() - timedelta(hours=3)
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=taker, claimed_at=claimed)
        refund = OrderRefund.objects.create(
            order=order, kind=OrderRefund.Kind.PARTIAL,
            amount=Decimal("1.00"), reason="测试原因甲", request_id="req-甲",
        )
        reversal = OrderRefund.objects.create(
            order=order, kind=OrderRefund.Kind.REVERSAL,
            amount=Decimal("-1.00"), reason="测试冲销", request_id="req-乙",
        )
        # ⚠️ **必须把两个时刻定死且让冲销那条更晚**（`created_at` 是 `auto_now_add` ⇒ 建完用
        #    `.update()` 覆写，`update()` 不触发 `auto_now_add`）：否则「排除 `REVERSAL`」这条口径
                #    无法与「不过滤、取最新一条」区分（实测：不强化的写法**杀不掉**该变异）。
        partial_at = timezone.now() - timedelta(days=2)
        reversal_at = timezone.now() - timedelta(days=1)
        OrderRefund.objects.filter(pk=refund.pk).update(created_at=partial_at)
        OrderRefund.objects.filter(pk=reversal.pk).update(created_at=reversal_at)
        row = self._data_rows(self.client_a.get(EXPORT_URL))[0]
        self.assertRegex(row[18], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(row[18], timezone.localtime(claimed).strftime("%Y-%m-%d %H:%M:%S"))
        # 第 25 列**必须等于接手人显示名**（`taker_name_expression()` 的注解真的装上去并且解析对了）。
        # ⚠️ 只断言第 19 / 21 列时，「注解保留、值换成空串」这条变异杀不掉（实测该变异 SURVIVED）。
        self.assertEqual(row[24], "测试打手乙")
        # 第 21 列**必须等于那条 `PARTIAL` 的时刻**（⛔ 不是更晚的 `REVERSAL`）——
        # 这就是「排除冲销类 + 取最近一条」两条口径的**正面判据**（能杀掉「去掉 `.exclude(REVERSAL)`」的变异）。
        self.assertEqual(
            row[20], timezone.localtime(partial_at).strftime("%Y-%m-%d %H:%M:%S"))
        # ⭐ 再加一条**反例腿**：只有 `REVERSAL` 退款时这一列**必须是空串**（⛔ 不是它的时刻）。
        only_reversal = self._order("20260927000000000092", game=self.game_a)
        OrderRefund.objects.create(
            order=only_reversal, kind=OrderRefund.Kind.REVERSAL,
            amount=Decimal("-1.00"), reason="测试冲销乙", request_id="req-丙",
        )
        # ⚠️ 端点腿的「空串」判据走 `_assert_empty_string_cell`（`cell.value` 读回区分不出
        #    空串与「没写」，见该方法的 docstring）。
        self._assert_empty_string_cell(
            self.client_a.get(EXPORT_URL), "20260927000000000092", 20)

    def test_a_set_refund_row_also_writes_the_refund_time(self):          # 第 21 列 / D-0312
        """⭐ 2026-10-26（D-0312）：`kind=SET` 的差额行也是真实退款事件 ⇒ 退款时间照常出。

        本用例同时钉住「该行的 `amount` 是**负数**（500 → 50 的差额）时导出不出错」。
        """
        order = self._order("20260927000000000093", game=self.game_a)
        refund = OrderRefund.objects.create(
            order=order, kind=OrderRefund.Kind.SET,
            amount=Decimal("-450.00"), reason="设置累计退款额", request_id="req-set-甲",
        )
        set_at = timezone.now() - timedelta(minutes=5)
        OrderRefund.objects.filter(pk=refund.pk).update(created_at=set_at)
        response = self.client_a.get(EXPORT_URL)
        self.assertEqual(response.status_code, 200)
        row = self._data_rows(response)[0]
        self.assertEqual(
            row[20], timezone.localtime(set_at).strftime("%Y-%m-%d %H:%M:%S")
        )

    def test_query_count_does_not_grow_with_rows(self):                  # B17
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for index in range(3):
            order = self._order(f"2026092700000000010{index}", game=self.game_a)
            Performance.objects.create(
                order=order, emp_type=EmpType.MAIN,
                user=User.objects.create_user(username=f"测试打手{index}", password="x"),
                claimed_at=timezone.now(),
            )
        with CaptureQueriesContext(connection) as few:
            self.client_a.get(EXPORT_URL)
        for index in range(3, 9):
            order = self._order(f"2026092700000000010{index}", game=self.game_a)
            Performance.objects.create(
                order=order, emp_type=EmpType.MAIN,
                user=User.objects.create_user(username=f"测试打手{index}", password="x"),
                claimed_at=timezone.now(),
            )
        with CaptureQueriesContext(connection) as many:
            self.client_a.get(EXPORT_URL)
        self.assertLessEqual(
            len(many.captured_queries), len(few.captured_queries),
            "SQL 条数随行数增长 ⇒ 接手时间 / 退款时间在逐行查库（spec §8-1 B17）",
        )

    def test_offer_price_redaction_writes_an_empty_string(self):         # B5b（端点腿）
        self._order("20260927000000000201", game=self.game_a)
        redacted = make_logged_in_client(
            username="测试导出脱敏", groups=(TEST_GROUP,),
            nodes=(NODE_EXPORT, "order.restrict_view_offer_price"),
            games=[self.game_a],
        )
        response = redacted.get(EXPORT_URL)
        self._assert_empty_string_cell(response, "20260927000000000201", 21)

    def test_missing_refund_time_is_written_as_an_empty_string(self):    # 第 21 列空值
        """第 21 列（退款时间）**没有退款**时必须是空串 `''`，不是 `None` / 空单元格。

        ⚠️ 补这条的原因（Task 3 评审实测）：变异「`_time_text(None)` 的 `return ""` →
        `return None`」此前在本套件里**全绿**——其余用例要么四个时间都非空，
        要么只断言金额 / 样式，恰好漏掉「时间列空值」这一支。
        """
        self._order("20260927000000000111", game=self.game_a)
        response = self.client_a.get(EXPORT_URL)
        self.assertEqual(response.status_code, 200)
        self._assert_empty_string_cell(response, "20260927000000000111", 20)

    def test_response_headers_carry_both_filenames(self):                # §5
        response = self.client_a.get(EXPORT_URL)
        disposition = response["Content-Disposition"]
        self.assertIn('filename="orders-', disposition)
        self.assertRegex(disposition, r"filename\*=UTF-8''")
        self.assertIn("xlsx", disposition)

    def test_over_the_row_cap_errors_instead_of_truncating(self):        # B8
        limit = 10000
        Order.objects.bulk_create([
            Order(
                game=self.game_a, server=self.server,
                order_no=f"2026092800000{i:07d}",     # 20 位、互不相同
                status=OrderStatus.IN_PROGRESS,
                total_price=Decimal("10.00"),
                refund_fee=Decimal("0.00"),
            )
            for i in range(limit)
        ])
        ok = self.client_a.get(EXPORT_URL)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok["Content-Type"], XLSX_MIME)
        self.assertEqual(workbook_of(ok)["Sheet1"].max_row, limit + 1)   # 表头 + 10000

        extra = self._order("20260928000009999999", game=self.game_a)
        self.assertIsNotNone(extra.pk)
        # ⛔ **报错不截断**：越限这一发**不是** xlsx（响应头与 body 都按信封判）。
        # ⛔ **越限那一路不许留痕**（spec §6-3 明写「超上限 / 参数畸形两条路……不记」）：
        #    判据取**前后各一次数据库行数**并断言不变（E-0009：副作用断言打在真实出口上，
        #    ⛔ 不打在 `logger` 这类中间层）。
                #    ⚠️ 这条是 实测补上的缺口：把 `record(...)` 挪到越限 `return`
        #    之前时，下面这条 `OperationLog.objects.count()` 不变断言**必须红**。
        logs_before_over_cap = OperationLog.objects.count()
        over = self.client_a.get(EXPORT_URL)
        self.assertEqual(OperationLog.objects.count(), logs_before_over_cap)
        self.assertNotEqual(over["Content-Type"], XLSX_MIME)
        body = over.json()
        self.assertEqual(body["code"], 1000)
        self.assertIn("10000", body["msg"])
        self.assertIn("超过上限", body["msg"])

    # ── 留痕（spec §6-3 / §8-1 B14）────────────────────────────────
    def test_successful_export_records_exactly_one_log(self):            # B14
        self._order("20260927000000000301", game=self.game_a)
        before = OperationLog.objects.count()
        self.client_a.get(EXPORT_URL)
        logs = OperationLog.objects.order_by("-id")[:1]
        self.assertEqual(OperationLog.objects.count(), before + 1)
        log = logs[0]
        self.assertEqual(log.action, "order.export")
        self.assertEqual(log.log_type, 10)
        self.assertEqual(log.actor, "测试导出甲")                        # 取**认证态**，不取请求参数
        # ⭐ 审计日志线（spec §3.3）：人为操作的正文带 `【显示名】`（该夹具用户没有
        # `first_name` ⇒ 显示名就是登录账号）。
        self.assertEqual(log.summary, "【测试导出甲】从订单管理列表导出了订单数据")   # 字面值（E-0024）
        self.assertEqual(log.target_kind, "")
        self.assertEqual(log.target_id, "")

    def test_rejected_exports_do_not_record(self):                       # B14 后半
        before = OperationLog.objects.count()
        self.client_a.get(EXPORT_URL + "?price_max=abc")                 # 参数畸形
        self.client_a.get(EXPORT_URL + "?payment_status=")               # 参数畸形
        self.assertEqual(OperationLog.objects.count(), before)

    def test_source_group_filter_is_forwarded_and_matches_the_list(self):
        """⭐ 公司店铺分组（2026-10-26，计划 §4 #3）：`?source_group=` **必须被转发**。

        判别力：把 `order_export_views` 里那行 `source_group=data.get("source_group")` 删掉 ⇒
        导出会多出**别组**那张单（本条第一半红）。列表侧的同一条件也断一次（两处同源）。
        ⚠️ 另有一张未分组来源的单做对照：没有它，「导出丢掉条件」在这条用例上会全绿。
        """
        group = OrderSourceGroup.objects.create(name="导出分组甲")
        in_group = OrderSource.objects.create(name="导出来源甲", group=group)
        out_group = OrderSource.objects.create(name="导出来源乙")
        no_in = "20260927000000000061"
        no_out = "20260927000000000062"
        self._order(no_in, game=self.game_a, order_source=in_group)
        self._order(no_out, game=self.game_a, order_source=out_group)

        rows = self._data_rows(self.client_a.get(EXPORT_URL + f"?source_group={group.pk}"))
        self.assertEqual([row[1] for row in rows], [no_in])

        listed = self.client_a.get(LIST_URL + f"?source_group={group.pk}").json()
        self.assertEqual(
            sorted(row["order_no"] for row in listed["data"]["items"]),
            [no_in],
        )

    def test_workbook_build_failure_is_a_loud_500_without_a_log(self):
        """⛔ **建文件失败 ⇒ 响亮 500 + 一行日志都不写**（spec §6-3「先建文件、再留痕」）。

        ⚠️ 补这条的原因（Task 4 评审实测 V1）：变异「把 `record(...)` 挪到建文件之前」在补这条
        之前**全绿**——既有的成功 / 拒绝两条用例都看不到「先建文件、再留痕」这个顺序。
        若将来有人把留痕上移，一次**根本没导出成功**的请求会先写下一条「已导出」的假日志，
        而调用方只看到 500（`AGENTS.md` §5-E：未预期异常仍要**响亮地 500**，
        ⛔ 不许伪装成业务结果，否则监控失明）。
        ⚠️ 桩打在**被查找的那个模块**上（E-0029）：视图里这个名字住在
        `apps.business.api.order_export_views`（它 `from apps.business.order_export import
        build_workbook`）⇒ 打在**视图模块**的 `build_workbook` 上，不是定义它的那个模块。
        ⚠️ `raise_request_exception = False` 是拿 500 响应的必需设置（Django 5.2 默认 `True`
        会把未预期异常重新抛进用例，断言就以 ERROR 收场、看不到 500）。
        """
        self._order("20260927000000000311", game=self.game_a)
        before = OperationLog.objects.count()
        # 对照组：**不接桩**时这一发是 200，且**会**记一条 ⇒ 证明下面的 500 是桩造成的（E-0029）。
        self.assertEqual(self.client_a.get(EXPORT_URL).status_code, 200)
        self.assertEqual(OperationLog.objects.count(), before + 1)

        self.client_a.raise_request_exception = False
        with mock.patch(
            "apps.business.api.order_export_views.build_workbook",
            side_effect=RuntimeError("测试：建文件失败"),
        ):
            response = self.client_a.get(EXPORT_URL)

        self.assertEqual(response.status_code, 500)
        # 没建出文件 ⇒ 一行日志都不许写（尤其⛔ 不许留「已导出」的假日志）。
        self.assertEqual(OperationLog.objects.count(), before + 1)
