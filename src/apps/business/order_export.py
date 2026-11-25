"""订单导出的 `.xlsx` 构造：**31 列定义 + 「`Order` → 单元格」纯映射 + 本线的 openpyxl 落点**。 ⚠️ **样式值不来自绩效导出的先例**：`performance_export.build_workbook` 一个样式都不设
（无着色 / 无行高 / 无列宽）；本模块的表头灰底 / 行高 / 列宽 / 居中**全部来自业务方给的格式参考件**（spec §5 的实测表）⇒ 后来者⛔ 不要以为「照绩效导出抄就对了」。 ⛔ **openpyxl 的调用点**：本模块是本仓第 2 个（2026-09-27 用
`grep -rn openpyxl --include=*.py src/` 枚举生产代码得到：此前只有 `apps/business/performance_export.py` 一处），也是**本线唯一一处**：列集 / 表头 / 形状只有这一份实现，视图与测试都别自己再建一个 workbook。"""
import re
from io import BytesIO

from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, PatternFill
from openpyxl.utils import get_column_letter

from apps.accounts.display import user_display_name
from apps.business.models import OrderStatus
from apps.business.performance_export import safe_cell_text
from apps.business.settlement_amount import net_received

#: 工作表名（参考件实测：单 sheet，名字 `Sheet1`）。
SHEET_TITLE = "Sheet1"

#: 时间列的格式（spec §3：四个时间列**统一到秒**，有意偏离参考件的分钟精度）。
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 换行压缩的判据（spec §3 第 6 行逐字）：每个 `\r\n` / `\r` / `\n` 各换成**一个**半角空格，
#: ⛔ **不**折叠连续空行（`\n\n` ⇒ 两个空格）⇒ 不用 `\s+`。
_NEWLINES = re.compile(r"\r\n|\r|\n")

#: 31 个表头——**顺序与文字逐字照参考件**，⛔ 不要漏列、不要改序（本线规定它是列集的唯一出处）。
ORDER_EXPORT_HEADERS = (
    "订单号",
    "系统内置单号",
    "来源",
    "游戏名称",
    "服务器",
    "格式化订单内容横排",
    "格式化订单内容",
    "自定义标记",
    "重要备注",
    "代练/陪玩项目",
    "录单人",
    "录单时间",
    "订单状态",
    "角色名",
    "接单价",
    "实收款",
    "退款",
    "游戏账号",
    "接手时间",
    "完成时间",
    "退款时间",
    "总计发单价",
    "发单人",
    "子副账号",
    "当前接手人",
    "接单客服",
    "电话",
    "角色信息",
    "项目分类",
    "旺旺",
    "QQ/微信",
)

#: 数值列的 0-based 下标（参考件实测：数值列宽 13.0、其余 20.0）⇒ 输出第 15 / 16 / 17 / 22 列。
NUMERIC_COLUMN_INDEXES = (14, 15, 16, 21)

#: 视图侧 `Subquery` 注解用这两个键（本模块的映射读它们；⛔ 视图必须真的注解上，
#: 缺了会 `AttributeError` 响亮地炸——这比静默出空串好）。
CLAIMED_AT = "export_claimed_at"
REFUNDED_AT = "export_refunded_at"

HEADER_ROW_HEIGHT = 32.0
DATA_ROW_HEIGHT = 25.0
HEADER_FILL_RGB = "FF808080"
COLUMN_WIDTH_NUMERIC = 13.0
COLUMN_WIDTH_TEXT = 20.0


def _time_text(value):
    """`datetime` → `"YYYY-MM-DD HH:MM:SS"`（本地时区）；空值 ⇒ 空串。"""
    if value is None:
        return ""
    return timezone.localtime(value).strftime(TIME_FORMAT)


def _name_of(related):
    """可空 FK → 名字；FK 为空 ⇒ 空串。"""
    return related.name if related is not None else ""


def row_values(order, *, can_view_offer_price, can_view_taker, created_by_names=None):
    """把一条 `Order` 映射成输出表的 31 个格（**纯函数**，不碰数据库）。 ⚠️ `can_view_offer_price` / `can_view_taker` 由调用方按**当前请求的 user** 算好传进来
    （`order_flags` 的两个判定）——本函数是纯映射，不自己读权限（否则没法单独对拍映射）。 ⛔ **空值一律写空串 `''`**（spec §3 开头的统一口径）：⛔ 不写 `None`、⛔ 不写 `0`
    （「没设」与「0」在本项目是两件事，D-0015）。 ⚠️ `created_by_names` = 视图侧批量算好的「登录账号 → 显示名」映射（`display_names_for_usernames`，一次查询）；
    `created_by` 列存的是**账号快照**（下游按账号消费，存储值不动），第 11 列导出时映射成显示名——查不到（已硬删 / 改名）回落原值。"""
    content = order.content or ""
    publisher = order.hall_publisher
    return [
        order.source_order_no or order.order_no,                      # 1 订单号（非空回落系统单号）
        order.order_no,                                               # 2 系统内置单号（⛔ 与第 1 列不同源）
        order.source or "",                                           # 3 来源（只读快照）
        _name_of(order.game),                                         # 4 游戏名称
        _name_of(order.server),                                       # 5 服务器（区服合一整串）
        _NEWLINES.sub(" ", content),                                  # 6 格式化订单内容横排
        content,                                                      # 7 格式化订单内容（原样）
        order.tag or "",                                              # 8 自定义标记
        order.remark or "",                                           # 9 重要备注
        order.project_text or "",                                     # 10 代练/陪玩项目
        (created_by_names or {}).get(order.created_by, order.created_by or ""),  # 11 录单人（显示名：快照账号映射，查不到回落原值）
        _time_text(order.created_at),                                 # 12 录单时间
        OrderStatus(order.status).label,                              # 13 订单状态
        order.game_role_name or "",                                   # 14 角色名
        order.total_price,                                            # 15 接单价
        net_received(order),                                          # 16 实收款（派生）
        order.refund_fee,                                             # 17 退款（无退款写 0）
        order.game_account or "",                                     # 18 游戏账号
        _time_text(order.export_claimed_at),                          # 19 接手时间
        _time_text(order.completed_at),                               # 20 完成时间
        _time_text(order.export_refunded_at),                         # 21 退款时间
        order.hall_price if (can_view_offer_price and order.hall_price is not None) else "",  # 22 总计发单价
        user_display_name(publisher) if publisher is not None else "",  # 23 发单人（显示名：昵称优先、空串回落账号）
        order.game_sub_account or "",                                 # 24 子副账号
        (order.taker_name or "") if can_view_taker else "",           # 25 当前接手人
        order.sales_agent or "",                                      # 26 接单客服
        order.customer_phone or "",                                   # 27 电话
        f"[{_name_of(order.game)}]{_name_of(order.server)}",          # 28 角色信息
        _name_of(order.category),                                     # 29 项目分类
        order.customer_ww or "",                                      # 30 旺旺
        order.customer_qq or "",                                      # 31 QQ/微信
    ]


def build_workbook(rows):
    """按参考件实测的形状与样式建单 sheet workbook，返回 `.xlsx` 的**字节**。样式（spec §5 实测）：表头行高 32.0 + 实心灰 `FF808080` + 居中；数据行行高 25.0 + 居中；
    列宽数值列 13.0 / 其余 20.0；**全表 `number_format` 保持默认 `General`**（⛔ 一格都不设）；
    不冻结、不自动筛选、不合并、不设边框与字体。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = SHEET_TITLE
    # 表头也过同一道收口（D-0334 起口径统一）：常量本就不以公式起始符开头 ⇒ 输出逐字不变，
    # 但「每一格都过助手」从此对本模块无条件成立。
    sheet.append([safe_cell_text(header) for header in ORDER_EXPORT_HEADERS])

    header_fill = PatternFill(fill_type="solid", fgColor=HEADER_FILL_RGB)
    centered = Alignment(horizontal="center", vertical="center")
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.alignment = centered
    sheet.row_dimensions[1].height = HEADER_ROW_HEIGHT

    for row in rows:
        # ⭐ E-0249：写格前的**唯一净化收口**——上游自由文本（remark / project_text / content …）当不可信输入，剔掉 openpyxl 拒写的 XML 非法控制字符（`0x00–0x08` / `0x0b` / `0x0c` / `0x0e–0x1f`），否则一行脏字符让整份导出 500。
        # ⛔ **复用**绩效导出的净化助手（同一份判据，不抄第二份）；`\t` / `\n` / `\r` 是合法字符、原样保留（列 6 的换行压缩与列 7 的原样语义都不受影响）。 ⭐ D-0334（业务方拍板「导出边界转义」）：同一收口换 `safe_cell_text`
        # ——**文本**以 `=` / `+` / `-` / `@` 开头 ⇒ 加 `'` 前缀（防 `=` 被 openpyxl 绑成公式：文本静默丢失、Excel 打开还会求值）；⛔ 数值格（含负数金额）原样。
        sheet.append([safe_cell_text(value) for value in row])
        for cell in sheet[sheet.max_row]:
            cell.alignment = centered
        sheet.row_dimensions[sheet.max_row].height = DATA_ROW_HEIGHT

    for index in range(len(ORDER_EXPORT_HEADERS)):
        sheet.column_dimensions[get_column_letter(index + 1)].width = (
            COLUMN_WIDTH_NUMERIC if index in NUMERIC_COLUMN_INDEXES else COLUMN_WIDTH_TEXT
        )

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
