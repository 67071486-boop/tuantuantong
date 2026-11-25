/**
 * 金额的**格式化**与**输入校验**。
 *
 * ⚠️ **金额可以为负**（退款 / 冲抵 / 调整）——业务方 2026-09-20 确认，见 D-0015。
 * **不得**在这里加任何「必须 ≥ 0」的校验：模型校验器 / 序列化器 `min_value` / `CHECK` 都不行，
 * 前端也不行。加回去就会把业务需要的退款 / 冲抵金额挡在门外。
 *
 * 后端判据**不是**「两位小数」一条，而是 `serializers.DecimalField(max_digits=12, decimal_places=2)`
 * （`src/apps/business/api/serializers.py`）的**四条**：格式（`Decimal` 可解析）、最多两位小数、
 * 量级（总位数 ≤ 12 且整数位 ≤ 10）、长度（strip 后 ≤ 1000 字符，DRF `MAX_STRING_LENGTH`）。
 * **没有符号约束**（见上）。
 *
 * ⚠️ **「空值怎么办」逐路径不同，不要在这里一刀切**——两条路径用的序列化器不一样，
 * 且下面这段是本模块最容易写错的地方（评审 Fix round 2 逐字段实测过）：
 *
 *  - **改价 `/price/`**：`PriceChangeSerializer.total_price` 是显式 `DecimalField`
 *    （`required=True, allow_null=False`）→ `''` / `'  '` 报 `invalid`、**省略报 `required`**、
 *    `null` 报 `null`。即**改价的金额是必填**，前端必须在行内拦住空值——
 *    否则只是把后端的 `invalid` 换成 `required`。
 *  - **录单 `POST /orders/`**：`OrderSerializer.total_price` 由模型生成，而模型里它已是
 *    **非空 + `default=Decimal("0.00")`**（P4-1，`models.py`）→ DRF 生成
 *    `required=False, allow_null=False`（实测）：**省略**则该字段被 `SkipField` 跳过后
 *    落模型默认 `0.00`，而 `''` / `'  '` / `null` **都是拒绝**（业务码 `1000`）。
 *    同一序列化器的 `received_price` / `platform_price` / `hall_price` / `fee` 仍是
 *    `null=True, blank=True` → 对它们 `''` / `'  '` / `null` 才**都落成 `None`**（接受）。
 *    即**录单的金额可省，但不是「一律可空」**——逐字段不同（已核 `OrderSerializer().fields`）。
 *
 *  机制在 DRF `fields.py` 的 `DecimalField.validate_empty_values`：**只有 `allow_null=True` 时**
 *  它才把空白串转成 `None`；`allow_null=False` 时空白串会落到 `to_internal_value` → `invalid`。
 *
 *  因此本函数只回答「这个字符串能不能作为一个金额提交」，对空白返回 `ok`（= 不填）；
 *  **「必填」是弹窗层的判断，不在这里**。调用方必须知道自己走的是哪条路径。
 */
export const MONEY_INPUT_PLACEHOLDER = '如 -199.50（可为负；退款 / 冲抵）'

/** `Order` 上的 6 个金额字段（后端 `models.MONEY_FIELDS`）——录单表单按它渲染价格分组。 */
export const ORDER_MONEY_FIELDS = [
  'total_price',
  'received_price',
  'platform_price',
  'hall_price',
  'refund_fee',
  'fee',
] as const

export type OrderMoneyField = (typeof ORDER_MONEY_FIELDS)[number]

/**
 * 6 个金额字段的中文名，**逐字**取自后端 `models.py` 的 `verbose_name`。
 * 放在这里（而不是 `api/orders.ts`）是为了让依赖方向保持单向：
 * `config/` 不 import `api/`——`api/orders.ts` 反过来 import 它。
 */
export const MONEY_FIELD_LABELS: Record<OrderMoneyField, string> = {
  total_price: '订单总价',
  received_price: '接单实收款',
  platform_price: '发平台价格',
  hall_price: '发内部抢单大厅价',
  refund_fee: '累计退款额',
  fee: '产生的手续费',
}

/** 显示值：**固定两位小数**；`null` / `undefined` / 空串 → `—`。
 *
 * ⚠️ `total_price` 自 P4-1 起**非空**（列默认 `0.00`），所以它不会再以 `null` 出现；
 * 但本函数仍把 `null` 映射成 `—`：另外几个金额字段（`received_price` / `platform_price` /
 * `hall_price` / `fee`）仍是可空的，而这里是**共用**的格式化函数。
 * 别据此写「总价可空」或「总价不可能是 0」——两种说法都与实现不符。
 *
 * ⚠️ 取整走 `Number().toFixed(2)`，即 IEEE-754 的**二进制近似**，不是十进制四舍五入：
 * `formatMoney('1.005')` 得 `'1.00'`（`1.005` 的二进制值略小于 1.005），
 * 而 `formatMoney('12.345')` 得 `'12.35'`——**两者都是「碰巧」而非设计**。
 * 对**已按最多两位小数校验/存储**的值（`validateMoneyInput` 与后端 `DecimalField` 的产物）
 * 没有影响；只有在拿原始的多位小数输入直接显示时才需要知道这一点。 */
export function formatMoney(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string' && value.trim() === '') return '—'
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n)) return '—'
  return n.toFixed(2)
}

/** 后端 `serializers.DecimalField(max_digits=12, decimal_places=2)` 的量级上限。
 *  整数位上限 = `max_digits − decimal_places` = 10（即 DRF 的 `max_whole_digits`）。
 *  已对着**运行中的** `PriceChangeSerializer` 实测过（见本任务报告「Fix round 1」）。 */
const MAX_DIGITS = 12
const MAX_WHOLE_DIGITS = 10
/** DRF `DecimalField.MAX_STRING_LENGTH` 的镜像（`fields.py`：先 strip，再判 `len > 1000`）。 */
const MAX_STRING_LENGTH = 1000

/**
 * Python `str.strip()`（= `str.isspace()` 为真的字符集）的**全部 29 个码点**——暴力穷举
 * 0..U+10FFFF 得到，不是凭印象写的。
 *
 * ⚠️ **不能用 `String.prototype.trim()` / `\s`**：两个集合不一样，且**两个方向都危险/有噪音**：
 *  - JS 的集合比 Python **多 `U+FEFF`**（BOM / ZWNBSP；`'\ufeff'.isspace()` 在 Python 里是
 *    `False`）。多剥 = **前端比后端松**（危险方向）：`'\ufeff12'` 后端 `invalid` 而前端会放行；
 *    `'\ufeff' + '0'×999 + '1'`（原始 1001 字符）还会**绕过长度上限**先被 trim 成 1000。
 *  - JS 的集合比 Python **少 `U+001C`-`U+001F` 与 `U+0085`**。少剥只会更严，但仍是噪音，
 *    这里一并对齐掉。
 *
 * 后端用的正是 Python 的 `smart_str(data).strip()`（DRF `fields.py:1050`、`1059`，以及
 * `DecimalField.validate_empty_values` 那处同款调用），所以这里按同一个集合逐字符裁剪，
 * 两边**逐字符相同**；「空白」这件事上不再有「近似」。
 */
const PY_STRIP_CHARS =
  '\u0009\u000a\u000b\u000c\u000d\u001c\u001d\u001e\u001f\u0020\u0085\u00a0\u1680' +
  '\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029' +
  '\u202f\u205f\u3000'

/** 与 Python `str.strip()` 逐字符等价的裁剪（码点集合见 `PY_STRIP_CHARS`）。 */
export function trimLikePython(value: string): string {
  let start = 0
  let end = value.length
  // 用 charAt（越界返回 ''）而不是 value[i]，免得踩 noUncheckedIndexedAccess。
  while (start < end && PY_STRIP_CHARS.includes(value.charAt(start))) start++
  while (end > start && PY_STRIP_CHARS.includes(value.charAt(end - 1))) end--
  return value.slice(start, end)
}

/**
 * 与 Python `str.strip()` 等价地判断「裁完是不是空串」（`''` 也算空）。
 *
 * 用途：录单页 `project_text` 的「必填」判断——后端 DRF `CharField` 默认 `trim_whitespace=True`，
 * 裁的就是这个空白集。**不要**改用 JS 的 `trim()`，它两个方向都会错：
 *  - `U+FEFF`（BOM）JS 裁、Python 不裁 → 前端把 `'\ufeff'` 当成空、按钮不可点，而后端接受它；
 *  - `U+001C`–`U+001F`、`U+0085` JS 不裁、Python 裁 → 前端放行，后端裁成空串后回 `code 1000`。
 *
 * 放在本模块（而不是各页面）是为了让空白集**只有一份** `PY_STRIP_CHARS`；页面 import 它即可，
 * 不要另抄一份表（抄了就会漂移）。
 */
export function isBlankLikePython(value: string): boolean {
  return trimLikePython(value) === ''
}

/**
 * 判据与后端 `serializers.DecimalField(max_digits=12, decimal_places=2)` 的差异**已枚举到 7 处**：
 * **更松 1 种**（整体由 Python 空白集组成的输入 → 后端拒、前端 `ok`）＋**更严 6 个形状**
 * （`.5` / `1.` / `+12` / `1e3` / `5e-1` / `1_000`）。**「已枚举 7 处」不等于「差异只有这 7 处」**——
 * 列表的完备性没有被证明，未记录的差异可能仍存在。不要改回「逐条对齐」那种绝对句。
 *
 * 与后端**一致**的判据：
 *  1. 十进制数字，允许前导 `-`（`12`、`-12`、`12.5`、`-12.50`）；
 *  2. 最多两位小数；
 *  3. **量级**：总位数 ≤ 12 且整数位 ≤ 10（忽略符号与前导零）。
 *     后端实测：`'12345678901'` → `max_whole_digits`（「请确保小数点前不超过 10 个数字」）、
 *     `'99999999999.99'` → `max_digits`（「请确保总计不超过 12 个数字」）、
 *     `'0001234567890.99'`（前导零不计）→ 合法；
 *  4. **长度**：trim 后 ≤ 1000 字符（DRF `MAX_STRING_LENGTH`）。后端实测 1000 合法、1001 报
 *     `max_string_length`；
 *  5. 首尾空白：**按 Python 的空白集**裁剪（见 `PY_STRIP_CHARS` / `trimLikePython`）——
 *     后端是 `smart_str(data).strip()`，`' 12 '` 合法。
 *
 * **与后端的两类差异，逐条枚举（本轮用「前端判定 vs 真后端判定」的穷举对拍确认过，见报告
 * 「Fix round 3」；这里不写「唯一」「任何」那种无法证伪的绝对句）：**
 *
 *  - **更松（1 种，刻意）**：输入在 Python 空白集内（含空串、纯空白、以及整体由 Python 空白
 *    组成者）→ 后端拒（改价 `required`/`invalid`），前端返回 `ok`（= 不填）。
 *    这是 brief 的设计：**「必填」由调用方按路径裁决**（见文件头逐路径那段）。
 *  - **更严（下面 6 个形状，刻意）**：`.5`、`1.`、`+12`、`1e3`、`5e-1`、`1_000` —— 后端实测
 *    **都合法**，前端内联拒掉，因为提示比「提交后由后端返回 `code 1000`」更早也更清楚。
 *    （`0x10`、千分位 `1,000`、`Infinity`、`NaN` 两边都拒，不算差异。）
 *
 * ⚠️ **`1_000` 是本清单的第 6 项，后补的，而且它曾把这里的一句绝对句证伪**：
 * 后端 `DecimalField` 走的是 `decimal.Decimal(data)`（**没有正则**），Python 的 `Decimal`
 * 接受下划线分隔（`Decimal('1_000') == Decimal('1000')`）→ 合法；前端正则拒。
 * 这属于 E-0011 那个模式（「除这两类外没有其他差异」这类句子能被一个反例证伪）。
 *
 * **本节不声称「差异只有上面这些」**：更严的形状按上面的列表**逐个实测**过，但**列表的完备性
 * 没有被证明**——要证明它得让语料按分支分层覆盖两侧。已知历史上还补过三处「更松」
 * （都在提交后才被后端打回，均已补齐）：缺量级上限（`'99999999999.99'` 放行）、
 * 缺长度上限（1001 个字符的 `'0'…01'` 放行）、用 JS `trim()` 会多剥 `U+FEFF`。
 * 未记录的差异**可能仍存在**；发现就补进来，别改回绝对句。
 *
 * `Number()` 的转换**不参与任何金额计算**，只用于「是不是有限数」这一条判据；
 * 提交的是用户输入的原始字符串，所以 `0.1 + 0.2` 那类浮点问题影响不到金额的显示与提交。
 */
export function validateMoneyInput(raw: string): { ok: true } | { ok: false; msg: string } {
  if (typeof raw !== 'string') return { ok: false, msg: '金额必须是文本' }
  const s = trimLikePython(raw)
  if (s === '') return { ok: true }
  // 长度判在格式之前——与 DRF `to_internal_value` 的顺序一致（先 strip，再判 `len`，最后 `Decimal`）。
  // 数的必须是裁剪后的 `s`：后端判的也是 strip 之后的长度。
  if (s.length > MAX_STRING_LENGTH) {
    return { ok: false, msg: `金额格式不正确：长度不能超过 ${MAX_STRING_LENGTH} 个字符` }
  }
  if (!/^-?\d+(\.\d{1,2})?$/.test(s)) {
    return { ok: false, msg: '金额格式不正确：只允许数字与最多两位小数（可为负）' }
  }
  // 位数极多的输入先在这里被拦下（正则不限制位数），保证下面的位数统计不会溢出；
  // 位数有限（≤12）时 Number() 必然有限，所以量级上限与这条判据互不重叠。
  if (!Number.isFinite(Number(s))) {
    return { ok: false, msg: '金额超出可表示范围' }
  }
  // 忽略符号与前导零后数位数——与 DRF `validate_precision` 用 `Decimal.as_tuple()` 的口径一致
  // （`Decimal('0001234567890.99')` 的 digits 是 12，不是 13）。
  const [wholePart = '', fractionPart = ''] = s.replace(/^-/, '').split('.')
  const wholeDigits = wholePart.replace(/^0+/, '').length
  const totalDigits = wholeDigits + fractionPart.length
  // 顺序与 DRF 一致：先 max_digits（总计），后 max_whole_digits（整数位）。
  // `'99999999999.99'` 两条都超，后端报的是 max_digits，这里必须报同一条。
  if (totalDigits > MAX_DIGITS) {
    return { ok: false, msg: '金额格式不正确：总计不能超过 12 位数字' }
  }
  if (wholeDigits > MAX_WHOLE_DIGITS) {
    return { ok: false, msg: '金额格式不正确：小数点前不能超过 10 位数字' }
  }
  return { ok: true }
}
