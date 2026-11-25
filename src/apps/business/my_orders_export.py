"""「我的订单收入」导出的 `.xlsx` 构造（D-0365）——本仓**第三个** openpyxl 落点。 **为什么单独一个模块**（照 `performance_export.py` / `order_export.py` 的同一形状）：列集 / 表头 / 单元格映射收在这里，视图（`api/my_orders_views.MyOrdersExportView`）只负责取数、上限判定与落响应头；⛔ openpyxl 只在本模块 `build_workbook` 里使用——视图与测试都别自己再建 workbook（「导出文件坏了」不会在别处报错）。 ⚠️ 本仓 openpyxl 落点的**枚举口径** = 「`src/apps/**/*.py` 里 import 了 openpyxl 的文件」，由
`tests/test_order_export_xlsx.py::ProductionOpenpyxlLandingPointTests` 钉住（本模块加入后是**恰好三个**；加第四个 / 少一个都红）。 **列集**（13 列，单工作表「收入记录」）：页面真有值的 10 列（用户名称 / 预计收入 / 实际收入 / 收入类型 / 系统内置单号 / 游戏 / 代练项目 / 订单状态 / 记录时间 / 结算状态） + 3 列对打手对账有用的载荷真值（来源单号 / 接手价 / 申请时刻）。 ⛔ **不加任何 PII 列**（客户联系方式 / 支付宝 / 收款码一概不出）。 **取值口径**（与列表载荷 `MyOrderSerializer` 同源，⛔ 不另写第二套）：用户名称 = `user_display_name`（昵称优先、空串回落账号；2026-11-05 业务方——账号不可直接见人）：
- 订单状态出**中文**（`OrderStatus` 的 `label`——后端词表的唯一出处是 `apps/business/models.py` 的 `OrderStatus.choices`）； - 接手价走 `performance_amount.effective_taker_price`（覆盖价优先、否则订单默认价），两边都没有价 ⇒ **空格**（同 `MyOrderSerializer.taker_price` 的 `None` 口径，⛔ 不写 `0.00`）； - 申请时刻 = 该行**自己的**提前结算申请时刻（反向一对一），`cancelled` 的申请回**空格** （作废之后可重申，显示「已申请过」会是假话——与 `MyOrderSerializer.settle_requested_at` 同一条口径）；时刻与「记录时间」同走本地时区文本（`TIME_FORMAT`）； - 写格统一过 `performance_export.safe_cell_text`（净化 + 公式起始符转义的**唯一**收口，本仓全部导出共用，⛔ 不在本模块各写一份）。"""
from io import BytesIO

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from openpyxl import Workbook

from apps.business.models import OrderStatus, PerformanceSettleRequest
from apps.business.performance_amount import MissingPriceError, effective_taker_price
from apps.accounts.display import user_display_name
from apps.business.performance_export import safe_cell_text

#: 单工作表的表名（与列集一样是本文件唯一出处）。
SHEET_TITLE = "收入记录"

#: 记录时间 / 申请时刻的单元格文本格式（与页面 `formatTime` 同形：`YYYY-MM-DD HH:MM`）。
TIME_FORMAT = "%Y-%m-%d %H:%M"

#: 13 列表头（顺序即列序；⛔ 视图与测试不要各自另抄一份）。
HEADERS = (
    "用户名称",
    "预计收入",
    "实际收入",
    "收入类型",
    "系统内置单号",
    "来源单号",
    "接手价",
    "游戏",
    "代练项目",
    "订单状态",
    "记录时间",
    "结算状态",
    "申请时刻",
)


def _local_text(value):
    """`datetime` → 本地时区的 `YYYY-MM-DD HH:MM` 文本；空值 → 空串。"""
    if value is None:
        return ""
    return timezone.localtime(value).strftime(TIME_FORMAT)


def _requested_at_text(performance):
    """该行自己的提前结算申请时刻；没有 / 已作废（`cancelled`）⇒ 空串。
    口径与 `MyOrderSerializer.settle_requested_at` 逐字同源（见那个字段的注释）：
    ⛔ 不按「这个人有没有申请过」判——那会把别人的 / 别的单的申请显示到这一行上。"""
    try:
        request_row = performance.settle_request
    except ObjectDoesNotExist:
        return ""
    if request_row.state == PerformanceSettleRequest.State.CANCELLED:
        return ""
    return _local_text(request_row.created_at)


def row_values(performance):
    """把一条 `Performance` 映射成收入表的 13 个格（**纯函数**，不碰数据库）。 ⚠️ 接手价**只吞 `MissingPriceError`**（两边都没价 = 这一格没有数 ⇒ 空串）；
    其它金额异常照抛（金额口径出了问题不是「没价」，E-0063 同族：按类型分档）。 ⚠️ 订单状态取 `OrderStatus(...).label`（中文）；未知枚举值会让这一步抛
    `ValueError` —— 那是数据坏了，照旧响亮地 500（不折成空串）。"""
    order = performance.order
    try:
        taker_price = effective_taker_price(performance)
    except MissingPriceError:
        taker_price = ""
    return (
        user_display_name(performance.user) if performance.user is not None else "",
        performance.temp_amount,
        performance.bonus_amount,
        performance.emp_type,
        order.order_no,
        order.source_order_no,
        taker_price,
        order.game.name,
        order.project_text,
        OrderStatus(order.status).label,
        _local_text(performance.created_at),
        performance.settle_status,
        _requested_at_text(performance),
    )


def build_workbook(rows):
    """单工作表「收入记录」按 13 列建好，返回 `.xlsx` 的**字节**（视图直接塞进 `HttpResponse`）。
    ⛔ **本函数是本仓第三个 openpyxl 调用点**（枚举口径见模块 docstring）；
    每一格都过 `safe_cell_text`（净化 + 公式起始符转义的唯一收口，与另两个导出同款）。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = SHEET_TITLE
    sheet.append([safe_cell_text(value) for value in HEADERS])
    for row in rows:
        sheet.append([safe_cell_text(value) for value in row])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
