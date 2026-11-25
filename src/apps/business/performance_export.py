"""绩效导出的 `.xlsx` 构造（Task 3）：**纯映射 + 本仓两个 openpyxl 落点之一**。 **四张工作表**（顺序即判据）：`绩效明细`（29 列）/ `绩效汇总`（10 列）—— 原有两张，表头与列序**逐字照附件**；`页一支付宝批量付款文件上传模板` / `打款跳过名单`——2026-10-16 新增的两张副表（口径见 `ALIPAY_*` / `SKIP_*` 常量与 `alipay_tables`，模板证据件在 `.tools/recon/alipay-payment-template/`）。 ⭐ **2026-10-22（业务方拍板「导出同步遮」）**：`build_workbook(can_view_order_money=False)` 时，明细表少「订单价格 / 退款」两列、汇总表少「总计订单价 / 总计退款」两列（29→27 / 10→8，表头与每一行同时少两格，⛔ 不是填 0 / 空串）。判据与列表/手机页 **同一处实现**：`apps/business/api/performance_views.can_view_order_money` （`order.list` 或 `finance.payroll`，任一即看）。 **有意偏离与解读（如实登记）**： - 打款表第 1 行 `A1:E1` 的**合并照做**（`build_workbook` 里 `merge_cells("A1:E1")`）——模板原件就是合并的，业务方口径是「按照这个格式」，而合并对上传解析无影响（支付宝只读 A–E 的表格区）。模板右侧的「填写说明」区（原文件 G1:Q16）**不搬**（同上：与解析无关）。 - 打款表的**金额格**多设了一个显示格式 `0.00`（`MONEY_FORMAT`）：模板说明要求「金额…精确到两位小数」，而 `Decimal` 的尾零**在数值里不携带**（写 `Decimal("55.00")` 读回就是 `55`） ⇒ 只有显示格式能把这个要求落下来。⚠️ **这是本表独有的偏离**：模板原件那几格是 `General`， **原有两张表一个单元格样式都不设**（⛔ 别把这套格式顺手铺到别的表上——那会改掉既有产物）。
- `\r`（`0x0d`）在净化面之外，但它的判据只能打在 `safe_cell_value` 这一层：`\r` 写进 xlsx 后读回会变成 `\n`——那是 XML 的换行归一（解析器行为），不是本仓的净化。所以 `\t` / `\n` 走端到端的格断言，`\r` 走函数层断言（见 `tests/test_performance_export_xlsx.py` 那条用例的 docstring，那里有实测形状）。 - ⚠️ **两条已知边界，按 spec §4.2 / §4.3 如实登记**（**出处 = 业务方 给的模板文件里那段说明原文**，逐字见 `.tools/recon/alipay-payment-template/`）： ① 支付宝的上传口**只吃 `.xls`（1997-2003 版本）或 CSV**，而本仓导出仍是 `.xlsx` ⇒ **真上传前要另存为 `.xls`**；⛔ 我们**不改扩展名、也不因此换格式**（换格式是另一件事）。 ② 模板原文「一个文件可以包含 3000 笔明细，超过 3000 笔可分多个文件上传」⇒ 本仓 **不截断、不分片**（本轮只登记；按账号汇总后 3000 人以上极罕见），⛔ 也**不**为了这条静默截断。 **为什么单独一个模块**：列集与表头不再从 `PerformanceListSerializer.Meta.fields` 派生（附件 `效绩提成记录*.xlsx` 是业务方自己从对标产品导出的**列集证据**，29 + 10 列与列表序列化器**不同集**）。把「订单/绩效 → 单元格」的映射与 xlsx 的构造细节收在这里： - 视图（`api/performance_write_views.PerformanceExportView`）只负责：取 queryset、查 `StaffProfile`、按用户名分组求和、把两类行交给本模块； - ⛔ **openpyxl 只在 `build_workbook` 里 import/使用**——别在视图或测试里各写一份 Excel 构造（两份必然漂移，而「导出文件坏了」不会在别处报错）。 ⚠️ **改准（订单导出线）**：本模块 docstring 原写「本仓只有这一处 openpyxl 落点」，
那句**已被证伪**（本线新增了第二个落点：`apps/business/order_export.py`）⇒ 现值是 **本仓两个落点**。**可复核的枚举口径** = 「`src/apps/**/*.py` 里 import 了 openpyxl 的文件」，由 `tests/test_order_export_xlsx.py` 的一条结构网钉住（恰好这两个模块；加第三个 / 少一个都红）。 **列的证据与落点**（brief §5.2 / §5.3，逐列给出处）： | # | 表头 | 来源 | |---|---|---| | 1 | 用户姓名/名称 | `user_display_name`（昵称优先、空串回落账号；2026-11-05 业务方） | | 2 | 预计额 | `temp_amount` | | 3 | 实计额 | `bonus_amount` | | 4 | 订单价格 | `order.total_price` | | 5 | 退款 | `order.refund_fee` | | 6 | 提成类型 | `emp_type`（库里的中文值） | | 7 | 关联订单 | `order.source_order_no` | | 8 | 接手人 | —（**无数据源，留空**） | | 9 | 发单价 | `order.hall_price`；`order.restrict_view_offer_price` 被施加时**留空** | | 10 | 系统内置单号 | `order.order_no` | | 11 | 游戏名称 | `order.game.name` | | 12 | 订单角色名 | `order.game_role_name` | | 13 | 职业 | —（留空） | | 14 | 项目 | `order.project_text` | | 15 | 订单来源 | `order.order_source.name`，为空回落 `order.source` | | 16 | 上传截图数 | —（留空） | | 17 | 排团编号 | —（留空） | | 18 | 接单客服 | `order.sales_agent` | | 19 | 订单状态 | `OrderStatus(order.status).label`（中文） | | 20 | 淘宝状态 | —（留空） | | 21 | 收款状态 | —（留空） | | 22 | 旺旺 | `order.customer_ww` | | 23 | 记录时间 | `performance.created_at`，`"MM-DD HH:MM"`（本地时区） | | 24 | 提交时间 | —（留空） | | 25 | 记录人 | `order.created_by` 账号快照 → 导出层映射显示名（`created_by_names`，查不到回落原值；2026-11-05 业务方） | | 26 | 清算状态 | `settle_status`（未结账 / 已结账） | | 27 | 清算时间 | `settled_at`，未结账 ⇒ 空 | | 28 | 录单时间 | `order.created_at` | | 29 | 账单备注 | —（留空） | ⚠️ **金额列写 `Decimal`，不要写 `str`**（附件实测：金额格是**数值格**，`t` 属性缺省）。 ⚠️ **时间列写字符串**（附件实测：那几格是共享字符串 `t="s"`）⇒ 我们按 `"MM-DD HH:MM"` 写字符串是**照附件**，不是将就。 ⚠️ **空列写空串 `""`**：openpyxl 把它落成**真空单元格**（读回是 `None`）。附件里「空」的格是 **空共享字符串**（读回 `''`）——两者在 Excel 里都是空格；brief 的补充说明只钉了「金额是数值格 / 时间是共享字符串」，**没有**要求空格的存储类型逐字节同构，所以不追求同构。"""
from decimal import Decimal
from io import BytesIO

from django.utils import timezone
from openpyxl import Workbook
#: ⚠️ **净化面直接取 openpyxl 自己那个正则对象**（⛔ 不在这里另抄一份字面量）：
#: openpyxl 的 `Cell._bind_value` 就是拿它判「这个字符串能不能写进 xlsx」，
#: 抄一份就会与它漂移（净化宽了 ⇒ 拒写的字符仍会让整份导出失败；窄了 ⇒ 悄悄改字）；本线要的是「**逐字保留除非法控制字符以外的一切**」。
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from apps.accounts.display import user_display_name
from apps.business.models import OrderStatus

#: 工作表名与顺序（附件：`绩效明细` 在前、`绩效汇总` 在后）。
DETAIL_SHEET_TITLE = "绩效明细"
SUMMARY_SHEET_TITLE = "绩效汇总"

#: 时间列的格式（附件逐格读出来的形状，如 `09-17 18:39`）。
TIME_FORMAT = "%m-%d %H:%M"

#: 明细表的 29 个表头——**顺序与文字逐字照附件**，⛔ 不要漏列、不要改序。
DETAIL_HEADERS = (
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
)

#: 汇总表的 10 个表头（同上，逐字照附件）。
SUMMARY_HEADERS = (
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
)

#: ⭐ **业务方报的越权展示 + 当天拍板「导出同步遮」**：不持「能看订单金额」判据（`performance_views.can_view_order_money`，两节点任一：`order.list` / `finance.payroll`）时，这两组列**整列不出现**——
#: 明细表的「订单价格 / 退款」、汇总表的「总计订单价 / 总计退款」。 ⛔ **不是填 0 / 空串**：那会让文件看起来「这两列是 0」，是对数据的假话；整列删掉，表头与每一行同时少两格（表头文字即定位依据，见 `_drop_columns`）。
#: ⚠️ 列名与附件逐字一致（`DETAIL_HEADERS` / `SUMMARY_HEADERS` 里的那四项）。
ORDER_MONEY_DETAIL_HEADERS = ("订单价格", "退款")
ORDER_MONEY_SUMMARY_HEADERS = ("总计订单价", "总计退款")


def _drop_columns(headers, rows, names):
    """按**表头文字**定位并整列删除（返回新的表头元组与行元组列表）。 ⚠️ **不按写死的下标**：列序一旦调整，按下标删会删错列，而且删错了不会报错
    （只是「钱那一格」跑到了别的表头下面）。按表头文字定位，`names` 拼错时这里**删不掉任何列**——判据由 `tests/test_performance_export_xlsx.py` 的
    表头/格断言钉住（拼错会当场红）。"""
    dropped = {index for index, header in enumerate(headers) if header in names}
    if not dropped:
        return headers, rows
    kept_headers = tuple(h for i, h in enumerate(headers) if i not in dropped)
    kept_rows = [tuple(v for i, v in enumerate(row) if i not in dropped) for row in rows]
    return kept_headers, kept_rows

# ── 需求 B：第 3 / 4 张副表 ──────────────────────────────────
# 第 3 张表的表名：**逐字**来自业务方 给的支付宝官方模板（`.tools/recon/alipay-payment-template/alipay-batch-payment-template.xls`；
# `.xlsx` 是 LibreOffice 转出来只读用的副本）。 ⚠️ 模板里还有一块 A–E 之外的「填写说明」区（原文件 G1:Q16）**不搬**：那是给人看的说明，支付宝只读 A–E 的表格区（spec §4.1）。
ALIPAY_SHEET_TITLE = "页一支付宝批量付款文件上传模板"
#: 第 1 行（模板里是 A1，合并 A1:E1）：**逐字**照模板，⛔ 不许改一个字。
ALIPAY_TITLE = "支付宝批量付款文件模板（前面两行请勿删除）"
#: 第 2 行的五个表头（模板 A2:E2）：**逐字**照模板，顺序也是判据。
#: ⚠️ 模板要求「前两行请勿删除」⇒ 数据一律从**第 3 行**开始（`build_workbook` 里就是
#: 先写 `ALIPAY_TITLE`、再写 `ALIPAY_HEADERS`、然后才是数据行）。
ALIPAY_HEADERS = (
    "序号（必填）",
    "收款方支付宝账号（必填）",
    "收款方姓名（必填）",
    "金额（必填，单位：元）",
    "备注（选填）",
)
#: 金额列的显示格式：模板说明原文是「须为正确表示金额的数字，**精确到两位小数**」。
#: ⚠️ 值本身仍是**数值格**（`Decimal`），本格式只决定 Excel 怎么显示它——
#: 不设它的话，`Decimal("55.00")` 在 Excel 里显示成 `55`（尾零是数值本身不携带的信息）。
MONEY_FORMAT = "0.00"

#: 第 4 张表的表名与 5 个表头（本仓自定，不是模板里的东西）：给财务看「谁没被打款」。
SKIPPED_SHEET_TITLE = "打款跳过名单"
SKIPPED_HEADERS = (
    "用户姓名/名称",
    "结算支付宝账号",
    "结算支付宝姓名",
    "跳过原因",
    "未打款金额",
)
#: 三种跳过原因（**逐字**，spec §3.3）：一个人命中多条 ⇒ 一行一条原因。
SKIP_REASON_NO_ACCOUNT = "没填结算支付宝账号"
SKIP_REASON_NO_NAME = "没填结算支付宝姓名"
SKIP_REASON_NON_POSITIVE = "合计金额不大于 0"
#: 跳过名单的行序判据（先按用户名、再按上表这个次序）——⛔ 不依赖字典/分组的偶然顺序。
_SKIP_REASON_ORDER = {
    SKIP_REASON_NO_ACCOUNT: 0,
    SKIP_REASON_NO_NAME: 1,
    SKIP_REASON_NON_POSITIVE: 2,
}


def safe_cell_value(value):
    """写格前的净化（需求 A / E-0242）：**剔掉字符串里的 XML 非法控制字符，其余逐字保留**。非法集合 = `0x00–0x08`、`0x0b`、`0x0c`、`0x0e–0x1f`（= openpyxl 的 `ILLEGAL_CHARACTERS_RE`，见上面的 import 注释）；⚠️ **`\\t`(`0x09`) / `\\n`(`0x0a`) /
    `\\r`(`0x0d`) 是合法字符，⛔ 不许一起删掉**。 ⛔ 只处理 `str`，其余类型（`Decimal` / `int` / `None` / `datetime`）**原样返回**—— 净化只回答「这个字符串能不能写进 xlsx」，不参与任何取值或类型转换。
    ⚠️ D-0334 起写格入口换成了 `safe_cell_text`（净化 + 公式起始符转义）；本函数仍是净化那一层的唯一实现，`safe_cell_text` 串联它，⛔ 不要绕开它另抄判据。"""
    if isinstance(value, str):
        return ILLEGAL_CHARACTERS_RE.sub("", value)
    return value


#: D-0334（二轮 Z13 / 五轮 M-08：xlsx 公式注入；业务方拍板「导出边界转义」）：表格公式起始符。以这些字符开头的**文本**写格前加 `'` 前缀——`=` 开头会被 openpyxl 的
#: `Cell._bind_value` 绑成公式（`data_type='f'`：文本静默丢失、Excel 打开还会求值）；
#: `+` / `-` / `@` 在 xlsx 本身不触发公式，但属拍板集合（防「另存 CSV 再打开」一类下游求值），一并转义。
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def safe_cell_text(value):
    """写格前的**出口转义**（D-0334）：`safe_cell_value` 的净化 + 公式起始符前缀。判据（业务方拍板）：**文本**若以 `=` / `+` / `-` / `@` 开头 ⇒ 前缀 `'`； ⛔ 非 `str`（`Decimal` / `int` / `None` / `datetime`）**原样返回**——负数金额是
    数值格，⛔ 不许被当文本转义（D-0015：金额 0 与负数都是合法且有意义的边界）； ⛔ 不以起始符开头的文本逐字保留（含净化）。 ⚠️ 串联次序：先净化（剔非法控制字符）再判起始符——前缀打在**净化后的结果**上（`"\\x0b=甲"` 净化成 `"=甲"`，仍要转义）。
    ⚠️ 转义的代价（拍板时已认）：这些格读回/显示多一个 `'`；正常值零变化。 ⚠️ 本仓两个导出（本模块 `_append_row` 与 `order_export.build_workbook`）都走它， ⛔ 不要在各处各写一份。"""
    value = safe_cell_value(value)
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _record_skip(skipped, username, account, name, reason, amount, display_name):
    """记一条跳过行，按 `(用户名, 原因)` **去重**（同一个人、同一原因只出一行）。 ⚠️ 为什么需要去重：`合计金额不大于 0` 这条原因有**两个触发面**——「本人这一份 ≤ 0」（逐人）
    与「他所在的账号分组合计 ≤ 0」（逐账号）。同一个人可能两面都命中，但**同原因只能一行** （spec §3.3 的「一行一因」说的是**不同的因**各一行）。
    ⚠️ 第 1 格存**显示名**（2026-11-05 业务方：导出人名列全改显示名）；去重键与行序仍按
    登录账号（见 `alipay_tables` 末尾的排序），⛔ 不换成分组键。"""
    skipped.setdefault((username, reason), (display_name, account, name, reason, amount))


def alipay_tables(bonus_rows, profiles, display_names=None):
    """按 `StaffProfile.alipay_account` 汇总出**打款表**与**跳过名单**两张表的数据行。 - `bonus_rows` = 当前筛选结果里**每一条明细行**的 `(用户名, 实计额)`；金额口径 = Σ `bonus_amount`（**生效绩效**，⛔ 不是 `temp_amount`）。 - `profiles` = `{用户名: StaffProfile}`（**由视图查好传进来**：本模块是纯映射，
    ⛔ 不自己查库——否则同一个事实会有两处取数）。 - `display_names` = 视图侧批量算好的「登录账号 → 显示名」（`display_names_for_usernames`，一次查询）；
    只换跳过名单第 1 格的输出，分组 / 合计 / 判定的键仍是登录账号；传 `None`（老调用方）时按原值输出。
    ⛔ 打款表（payable 行）**不动**：收款账号 / 收款姓名是真实付款数据，不属人名列。 - 一个账号一行（`序号` 从 1 递增的**整数**）；金额 ≤ 0 / 账号或姓名为空的人**不进打款表**，转而去第 4 张表登记原因。 - **一行一因**（spec §3.3）：三条原因**各自独立判定**，一个人**最多三行**—— `没填结算支付宝账号`（账号为空）与 `没填结算支付宝姓名`（姓名为空）**与另一条填没填无关**；
    `合计金额不大于 0` 有**两个触发面**：**本人这一份 ≤ 0**（逐人判）与 **他所在的账号分组合计 ≤ 0**（逐账号判，即「整个账号打不出去」）。 ⚠️ 同一个人**同原因只出一行**（两个触发面都命中时去重，见 `_record_skip`）。 - 同一个账号出现两个不同 `alipay_name` ⇒ 取**名称排序后的第一条**（`min`，与行序无关，输出可复现）；真实数据里若出现，属业务数据问题。返回 `(打款行, 跳过行)`；两张表的行都是**普通元组**，落格由 `build_workbook` 负责。"""
    names = display_names or {}

    def display_of(username):
        return names.get(username, username)

    user_total = {}
    for username, bonus_amount in bonus_rows:
        user_total[username] = user_total.get(username, Decimal("0.00")) + bonus_amount

    accounts = {}
    skipped = {}
    for username in sorted(user_total):
        profile = profiles.get(username)
        account = profile.alipay_account if profile is not None else ""
        name = profile.alipay_name if profile is not None else ""
        total = user_total[username]
        if not account:
            _record_skip(
                skipped, username, "", name, SKIP_REASON_NO_ACCOUNT, total,
                display_of(username),
            )
        if not name:
            _record_skip(
                skipped, username, account, "", SKIP_REASON_NO_NAME, total,
                display_of(username),
            )
        if total <= 0:
            # ⭐ 「本人这一份 ≤ 0」**与账号 / 姓名填没填无关**：没账号 / 没姓名的人压根不进下面的
            #    账号分组，光靠分组那条判据够不着他 ⇒ 他会漏登记（这正是「一行一因」要防的）。
            _record_skip(
                skipped, username, account, name, SKIP_REASON_NON_POSITIVE, total,
                display_of(username),
            )
        if account and name:
            group = accounts.setdefault(
                account, {"names": [], "total": Decimal("0.00"), "users": []}
            )
            group["names"].append(name)
            group["total"] += total
            group["users"].append(username)

    payable = []
    for account in sorted(accounts):
        group = accounts[account]
        if group["total"] <= 0:
            # ⚠️ 这条原因判在**账号分组**上（与打款表同一粒度）：一个人自己的合计可能是正的，
            # 但他的账号与别人合在一起不高于 0 ⇒ 整笔都打不出去，他也必须出现在名单里（否则就是「静默漏人」）。
            # ⚠️ 成员若本人也 ≤ 0，上面已记过**同原因**那一行 ⇒ 这里由 `_record_skip` 去重。
            for username in group["users"]:
                profile = profiles[username]
                _record_skip(
                    skipped,
                    username,
                    account,
                    profile.alipay_name,
                    SKIP_REASON_NON_POSITIVE,
                    user_total[username],
                    display_of(username),
                )
            continue
        payable.append(
            (len(payable) + 1, account, min(group["names"]), group["total"], "")
        )

    # ⚠️ 行序仍按**登录账号**升序 + 原因序（与显示名无关）：跳过行的第 1 格已是显示名，
    #    ⛔ 不能拿它当排序键——那会把「先按用户名」的行序判据悄悄换掉。所以按字典键（含用户名）排序。
    rows = [
        row
        for _, row in sorted(
            skipped.items(),
            key=lambda item: (item[0][0], _SKIP_REASON_ORDER[item[1][3]]),
        )
    ]
    return payable, rows


def _local_text(value):
    """`datetime` → `"MM-DD HH:MM"`（**本地时区**）；空值 → 空串（附件里空是空串）。"""
    if value is None:
        return ""
    return timezone.localtime(value).strftime(TIME_FORMAT)


def detail_values(performance, *, can_view_offer_price, created_by_names=None):
    """把一条 `Performance` 映射成明细表的 29 个格（**纯函数**，不碰数据库）。 ⚠️ `can_view_offer_price` 由调用方按**当前请求的 user** 算好传进来（`order_flags
    .can_view_offer_price`）——本函数是纯映射，不自己读权限（否则没法单独对拍映射）。 ⛔ **脱敏是留空，不是 `0`、也不是 `None`**：附件里没有这一格的「隐藏」形状，
    而我们这一列的必要条件只是「不要让人看见钱」（分不清「没有」与「被隐藏」可接受）。 ⚠️ 调用方必须 `select_related("order__game", "order__order_source")`：本函数逐行读那两个关联，不 select_related 时是 N+1。
    ⚠️ `created_by_names` = 视图侧批量算好的「登录账号 → 显示名」映射（`display_names_for_usernames`，一次查询）；
    「记录人」列存的是**账号快照**（`order.created_by`，下游按账号消费，存储值不动），导出时映射成显示名——查不到（已硬删 / 改名）回落原值；
    传 `None`（老调用方）时按原值输出。"""
    order = performance.order
    game = order.game
    source = order.order_source
    return (
        user_display_name(performance.user) if performance.user_id else "",
        performance.temp_amount,
        performance.bonus_amount,
        order.total_price,
        order.refund_fee,
        performance.emp_type,
        order.source_order_no,
        "",                                                     # 接手人：无数据源
        order.hall_price if can_view_offer_price else "",       # 发单价（受 flag 时留空）
        order.order_no,
        game.name if game is not None else "",
        order.game_role_name,
        "",                                                     # 职业：无数据源
        order.project_text,
        source.name if source is not None else order.source,
        "",                                                     # 上传截图数：无数据源
        "",                                                     # 排团编号：无数据源
        order.sales_agent,
        OrderStatus(order.status).label,
        "",                                                     # 淘宝状态：无数据源
        "",                                                     # 收款状态：无数据源
        order.customer_ww,
        _local_text(performance.created_at),
        "",                                                     # 提交时间：无数据源
        (created_by_names or {}).get(order.created_by, order.created_by or ""),  # 记录人（显示名：快照账号映射，查不到回落原值）
        performance.settle_status,
        _local_text(performance.settled_at),
        _local_text(order.created_at),
        "",                                                     # 账单备注：无数据源
    )


def summary_values(username, totals, profile, display_name=None):
    """把「一个用户名的分组合计 + 它的 `StaffProfile`」映射成汇总表的 10 个格。 - `totals` 的口径 = **明细行按用户名分组**：`count` = 行数，后四项 = `Σ`
    （与附件逐字对拍过的口径）。键名由调用方（视图）在累计时写死，见那里。 - `profile` 为 `None`（该用户没有档案）⇒ 结算四列**留空**，⛔ 不回落、不编造。
    - 第 1 格 = **显示名**（2026-11-05 业务方：导出人名列全改显示名）；`display_name` 缺省 / 空回落
    `username`（`display_names_for_usernames` 已含回落，这里再兜一层是防调用方漏传）；分组键仍按用户名。"""
    return (
        display_name if display_name else username,
        totals["count"],
        totals["temp_amount"],
        totals["bonus_amount"],
        totals["order_price"],
        totals["refund_fee"],
        profile.alipay_account if profile is not None else "",
        profile.alipay_name if profile is not None else "",
        profile.bank_name if profile is not None else "",
        profile.bank_card_no if profile is not None else "",
    )


def _append_row(sheet, values, *, money_column=None):
    """把一个数据行写进某张表：**所有格都先过 `safe_cell_text`**（唯一写格收口）。
    `safe_cell_text` = 需求 A 的净化（`safe_cell_value`）+ D-0334 的公式起始符转义。
    `money_column`（1 起）指定的那一格再设上两位小数的显示格式（见 `MONEY_FORMAT`）。"""
    sheet.append([safe_cell_text(value) for value in values])
    if money_column is not None:
        sheet.cell(row=sheet.max_row, column=money_column).number_format = MONEY_FORMAT


def build_settlement_workbook(rows):
    """生成与结算明细一致的工作表。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "结算明细"
    _append_row(sheet, ("结算对象人员", "报酬类型", "预计结算金额", "实际结算金额", "关联订单", "项目", "结算时间", "结算人"))
    for row in rows:
        _append_row(sheet, row)
        sheet.cell(row=sheet.max_row, column=3).number_format = "0.00"
        sheet.cell(row=sheet.max_row, column=4).number_format = "0.00"
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_workbook(detail_rows, summary_rows, bonus_rows, profiles, *, can_view_order_money, display_names=None):
    """四个工作表按顺序建好，返回 `.xlsx` 的**字节**（视图直接塞进 `HttpResponse`）。顺序与表名：`绩效明细`（29 列）/ `绩效汇总`（10 列）—— 原有两张，⛔ 表头/列序/值不动； `页一支付宝批量付款文件上传模板`（打款表）/ `打款跳过名单`—— 2026-10-16 新增的两张副表。 ⭐ **2026-10-22：`can_view_order_money=False` ⇒ 明细「订单价格 / 退款」与汇总
    「总计订单价 / 总计退款」整列删掉**（明细 27 列、汇总 8 列；打款/跳过两张副表不动）。 ⛔ 不是填 0 / 空串——整列不出现（判据与理由见 `performance_views.can_view_order_money`）。 ⚠️ 本参数是**必填关键字**（没有默认值）：调用方（视图）必须显式按请求者算一次， ⛔ 不许「忘了传就当能看」——那正是本条要关的口子。
    ⚠️ `display_names` 透传给 `alipay_tables`（跳过名单第 1 格换显示名用；`None` = 按原值输出，老调用方不受影响）。
    ⛔ **本函数是本仓两个 openpyxl 调用点之一**（另一个在 `apps/business/order_export.py`；枚举口径见模块 docstring）：列集/表头/形状只有这一份实现，视图与测试都别自己再建一个 workbook。 ⛔ **也是本模块唯一写格处** ⇒ 需求 A 的净化与 D-0334 的公式起始符转义（合起来 = `safe_cell_text`）只在这里、只调一次，原有两张表的每一格也同样过它（写进去的字符串与从前逐字相同，除非法控制字符；以 `=`/`+`/`-`/`@` 开头的文本多一个 `'` 前缀——业务方拍板口径）。"""
    if not can_view_order_money:
        detail_headers, detail_rows = _drop_columns(
            DETAIL_HEADERS, detail_rows, ORDER_MONEY_DETAIL_HEADERS
        )
        summary_headers, summary_rows = _drop_columns(
            SUMMARY_HEADERS, summary_rows, ORDER_MONEY_SUMMARY_HEADERS
        )
    else:
        detail_headers, summary_headers = DETAIL_HEADERS, SUMMARY_HEADERS

    workbook = Workbook()
    detail = workbook.active
    detail.title = DETAIL_SHEET_TITLE
    _append_row(detail, detail_headers)
    for row in detail_rows:
        _append_row(detail, row)

    summary = workbook.create_sheet(SUMMARY_SHEET_TITLE)
    _append_row(summary, summary_headers)
    for row in summary_rows:
        _append_row(summary, row)

    payable_rows, skipped_rows = alipay_tables(bonus_rows, profiles, display_names)

    alipay = workbook.create_sheet(ALIPAY_SHEET_TITLE)
    _append_row(alipay, [ALIPAY_TITLE])
    #: 模板原件第 1 行就是**合并 A1:E1** ⇒ 照做（对支付宝的上传解析无影响，见模块 docstring
    #: 的「有意偏离与解读」）。⚠️ 合并只在这张表、只在这一行。
    alipay.merge_cells("A1:E1")
    _append_row(alipay, ALIPAY_HEADERS)
    for row in payable_rows:
        _append_row(alipay, row, money_column=4)          # 第 4 列 = 金额

    skipped = workbook.create_sheet(SKIPPED_SHEET_TITLE)
    _append_row(skipped, SKIPPED_HEADERS)
    for row in skipped_rows:
        _append_row(skipped, row, money_column=5)         # 第 5 列 = 未打款金额

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
