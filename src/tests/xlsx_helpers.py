"""绩效导出 `.xlsx` 的读取夹具（Task 3）。

**为什么共享**：四份后端测试（`test_performance_export_xlsx` /
`test_p4fu_performance_writes` / `test_emp_emplist_backend` / `test_p4fu_performance_reads`）
都要把导出的**响应字节**读回成单元格；各写一份必然漂移（本仓对「同一事实两套判据」有成文教训）。

⚠️ **导出是 xlsx（zip 字节）⇒ 从前 `response.content.decode("utf-8-sig")` +
`assertIn(order_no, text)` 的判据**不再成立**（会 `UnicodeDecodeError`，或对着 zip 头做子串
匹配而恒假）。「某个订单号在不在文件里」这类判据必须按**单元格值**做 ⇒ 用 `values_of()`。

⚠️ **数值格的读回形状**（控制器补充，openpyxl 3.1.5 实测）：写 `Decimal("123.00")` 读回是
`int 123`、写 `Decimal("4.40")` 读回是 `float 4.4` ⇒ 比较一律 `Decimal(str(cell.value))`，
⛔ 不要 `assertEqual(cell.value, Decimal("4.40"))`（那是假红）。
"""
from io import BytesIO

from openpyxl import load_workbook

#: 两个工作表名（与 `apps/business/performance_export.py` 的常量同值；测试里可写字面值对拍）。
DETAIL_SHEET = "绩效明细"
SUMMARY_SHEET = "绩效汇总"


def workbook_of(response):
    """一次导出响应的 `content` 字节 → openpyxl 工作簿（`data_only=True`）。"""
    return load_workbook(BytesIO(response.content), data_only=True)


def rows_of(workbook, sheet):
    """某工作表的二维值矩阵（含表头行）。"""
    return [[cell.value for cell in row] for row in workbook[sheet].iter_rows()]


def detail_rows(workbook):
    """`绩效明细` 的二维值矩阵（第 0 行是表头）。"""
    return rows_of(workbook, DETAIL_SHEET)


def summary_rows(workbook):
    """`绩效汇总` 的二维值矩阵（第 0 行是表头）。"""
    return rows_of(workbook, SUMMARY_SHEET)


def values_of(workbook, sheet):
    """该表**数据行**里所有非空格的 `str` 值（跳过表头）。

    「某个订单号在不在明细里」这类判据用它 —— ⛔ 不要再对响应字节做 `in` / `decode`。
    """
    return [
        str(value)
        for row in rows_of(workbook, sheet)[1:]
        for value in row
        if value is not None
    ]
