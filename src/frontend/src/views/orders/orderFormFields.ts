import {
  MONEY_FIELD_LABELS,
  ORDER_MONEY_FIELDS,
  formatMoney,
  type OrderMoneyField,
} from '@/config/money'
import type { CreateOrderPayload, ParsedFields } from '@/api/orders'
import { useSessionStore } from '@/stores/session'

/** 表单模型。金额也是字符串：输入的原始文本直接提交，不做浮点转换。 */
export type OrderForm = {
  game: number | null
  server: number | null
  category: number | null
  project: number | null
  project_text: string
  content: string
  demand: string
  time_requirement: string
  game_account: string
  game_sub_account: string
  game_role_name: string
  current_info_1: string
  current_info_2: string
  customer_phone: string
  customer_qq: string
  customer_ww: string
  payment_method: string
  is_urgent: boolean
  tag: string
  remark: string
  source: string
  /**
   * 来源字典的主键（D3/D4）。**后端**据此覆盖写 `source` 名字——前端**不传**名字。⚠️ `null` 表示「没选来源」，`formToPayload` 会把它**丢掉**（空值不发）⇒ 请求体里没有这个键；这与「传了 0」不是同一件事（E-0021：`0` 不是合法来源 id，别用真值判）。
   * ⚠️ 唯一例外在编辑态：`order-addorder.ts::onSave` 在「进来时有来源、用户清空了」时**显式**补一个 `order_source: null`（spec 的 FK 置空语义）——`formToPayload` 本身仍然不含这个逻辑，别把补键挪进来（那会让录单态也发 `null`）。
   */
  order_source: number | null
  source_order_no: string
  sales_agent: string
} & Record<OrderMoneyField, string>

export function emptyOrderForm(): OrderForm {
  const money = Object.fromEntries(ORDER_MONEY_FIELDS.map((f) => [f, ''])) as Record<
    OrderMoneyField,
    string
  >
  return {
    game: null,
    server: null,
    category: null,
    project: null,
    project_text: '',
    content: '',
    demand: '',
    time_requirement: '',
    game_account: '',
    game_sub_account: '',
    game_role_name: '',
    current_info_1: '',
    current_info_2: '',
    customer_phone: '',
    customer_qq: '',
    customer_ww: '',
    payment_method: '',
    is_urgent: false,
    tag: '',
    remark: '',
    source: '',
    order_source: null,
    source_order_no: '',
    sales_agent: '',
    ...money,
  }
}

/**
 * 右栏的**分组与顺序**：逐个对照 P2 spec §5.2（`docs/specs/2026-09-19-p2-order-module-design.md` 的六个分组名与箭头顺序）——六个都在、顺序一致，不重排、不合并。
 * 抽成数据而不是在模板里堆六个 `el-card`：详情页要按同一分组只读呈现，两处必须同源。
 */
export const ORDER_FORM_GROUPS: { title: string; fields: string[] }[] = [
  { title: '订单内容', fields: ['content', 'demand', 'time_requirement'] },
  { title: '基本', fields: ['game', 'server', 'category', 'project', 'project_text'] },
  {
    title: '玩家账号',
    fields: ['game_account', 'game_sub_account', 'game_role_name', 'current_info_1', 'current_info_2'],
  },
  { title: '价格', fields: [...ORDER_MONEY_FIELDS, 'payment_method'] },
  { title: '客户联系', fields: ['customer_phone', 'customer_qq', 'customer_ww'] },
  {
    title: '其他',
    fields: ['is_urgent', 'source', 'source_order_no', 'sales_agent', 'tag', 'remark'],
  },
]

/**
 * 中文名对照后端 `models.py` 的 `verbose_name`（多数写在字段构造函数的第一个位置参数上，`grep verbose_name=` 只会撞到 7 个关键字写法）。本文件 **37 个键** = `Order` 的 **35 个有标签的具名字段** + 解析器回的 `game_name` / `server_name`。
 * ⚠️ **口径（三个数一起看，别只改其中一处）**：`Order` 模型有 **37 个具名字段**（逐条数出来，不含自动主键 `id`）；其中 35 个在本文件有标签，**未收录的具名字段只有 `order_no` / `status`**（详情页页头渲染，不需要正文标签）。序列化器 `exclude=[]` 还会输出**隐式主键 `id`**（模型字段因此是 38 个），但它没有 `verbose_name`、也不在本文件的键里——别说「未收录的**列**只有那两个」。
 * **与后端不同的只有两处**（为了把「配置里的项目」与「自由文本项目」区分开）：`project` → `'代练/陪玩项目（配置）'`、`project_text` → `'代练/陪玩项目'`。6 个金额字段的中文名直接展开 `@/config/money` 的 `MONEY_FIELD_LABELS`（同一事实只留一份）；`current_info_1/2` 后端未定义语义，照原样保留。
 */
export const FIELD_LABELS: Record<string, string> = {
  content: '客户原始需求',
  demand: '需求描述（打手看的）',
  time_requirement: '时间要求',
  game: '游戏',
  server: '区服',
  category: '项目分类',
  project: '代练/陪玩项目（配置）',
  project_text: '代练/陪玩项目',
  game_account: '游戏账号',
  game_sub_account: '子账号',
  game_role_name: '角色名',
  current_info_1: '当前信息1',
  current_info_2: '当前信息2',
  // 6 个金额字段的中文名来自 `@/config/money`（**唯一出处**）；这里展开，不重抄。
  ...MONEY_FIELD_LABELS,
  payment_method: '支付方式',
  customer_phone: '联系电话',
  customer_qq: 'QQ/微信',
  customer_ww: '旺旺/YY',
  is_urgent: '是否加急',
  source: '订单来源',
  source_order_no: '来源单号',
  sales_agent: '接单销售客服',
  tag: '自定义标记',
  remark: '重要备注',
  // 5 个「记录」字段：**录单表单不提交它们**（`ORDER_FORM_GROUPS` 里没有），但它们是订单记录的一部分，
  // 详情页的「记录」组要显示。中文逐字取自后端 `models.py`：`order_type`→`"订单类型"`、`created_by`→`"创建人"`、
  // `updated_by`→`"最后修改人"`、`created_at`→`"创建时间"`、`updated_at`→`"最后修改时间"`。
  order_type: '订单类型',
  created_by: '创建人',
  updated_by: '最后修改人',
  created_at: '创建时间',
  updated_at: '最后修改时间',
  // 解析器回的名称类字段里，**不是** Order 列的只有两个：`game_name` / `server_name`
  // （`intake_rules.py` 里以 `_name` 结尾的还有一个 `game_role_name`，它是**真列**，标签在玩家账号组）。
  // 它俩只能用来在下拉选项里找对应项；找不到就如实告诉客服，而不是静默丢掉。
  game_name: '游戏名称',
  server_name: '区服名称',
}

/**
 * `unmatched` 里的字段名 → 中文。**不认识的名字原样返回**：后端加了新规则而前端还没同步时，
 * 显示一个生字段名也比显示 `undefined` 强，而且一眼能看出是漂移。
 */
export function fieldLabel(name: string): string {
  return FIELD_LABELS[name] ?? name
}

/** 粘贴解析结果 → 可以直接写进表单的赋值（**只含解析出来的项**）。 */
export type PasteTargets = {
  /** 直接写进文本 / 金额字段（金额已格式化成两位小数）。 */
  scalars: Partial<Record<string, string>>
  /** 写进 FK：需要在下拉选项里按**精确同名**查找——`game`（`game_name`）/ `server`（`server_name`）解析器回的是**名称**；`orderSource`（`source`）是来源**名字**字符串（不是 id）。
   *  ⚠️ 解析器不回 `category` / `project`，所以这两个键既不产出也不消费（曾挂在类型上误导读者，已删）。 */
  byName: {
    game?: string
    server?: string
    /** 订单来源名：命中则写 `order_source`（FK id）；找不到 ⇒ 进 `unresolved`。 */
    orderSource?: string
  }
}

export function pasteFillTargets(fields: ParsedFields): PasteTargets {
  const scalars: Partial<Record<string, string>> = {}
  if (fields.game_account !== undefined) scalars.game_account = fields.game_account
  if (fields.game_sub_account !== undefined) scalars.game_sub_account = fields.game_sub_account
  if (fields.game_role_name !== undefined) scalars.game_role_name = fields.game_role_name
  if (fields.customer_phone !== undefined) scalars.customer_phone = fields.customer_phone
  if (fields.customer_qq !== undefined) scalars.customer_qq = fields.customer_qq
  if (fields.project_text !== undefined) scalars.project_text = fields.project_text
  // 金额走 `formatMoney`：/parse/ 已经回两位小数字符串，再走一次是**幂等**的；
  // 它同时兜住「后端某天回了数字型」的情况，且负数照样保留（D-0015）。
  if (fields.total_price !== undefined) scalars.total_price = formatMoney(fields.total_price)
  if (fields.received_price !== undefined) scalars.received_price = formatMoney(fields.received_price)
  // T8：配置驱动识别项的 4 个 **scalar**（同名直写）——它们有对应的表单控件，
  // 由页面的 `writeFormState` 按 `FIELD_MAP` 推回 DOM。
  if (fields.current_info_1 !== undefined) scalars.current_info_1 = fields.current_info_1
  if (fields.current_info_2 !== undefined) scalars.current_info_2 = fields.current_info_2
  if (fields.customer_ww !== undefined) scalars.customer_ww = fields.customer_ww
  if (fields.source_order_no !== undefined) scalars.source_order_no = fields.source_order_no

  const byName: PasteTargets['byName'] = {}
  if (fields.game_name !== undefined) byName.game = fields.game_name
  if (fields.server_name !== undefined) byName.server = fields.server_name
  // T8：`source` 回的是**名字**，不能当 scalar 直写（`order_source` 要的是字典 id）。
  if (fields.source !== undefined) byName.orderSource = fields.source
  return { scalars, byName }
}

export type ConfigOption = { id: number; name: string }

export type OptionDicts = {
  games: ConfigOption[]
  servers: ConfigOption[]
  categories: ConfigOption[]
  projects: ConfigOption[]
  /**
   * 来源字典：`source` 按**精确同名**在这里找 id。
   * ⚠️ 录单页传的是**运行期已加载**的那份（`fetchOrderSources()` 的结果，`order-addorder.ts` 的 `sourceOptions`）——传空数组等于让来源永远进 `unresolved`。
   */
  orderSources: ConfigOption[]
}

/** 按**精确同名**查找；找不到返回 `null`（**不猜**、不取第一个）。 */
function findByExactName(options: ConfigOption[], name: string): number | null {
  const hit = options.find((o) => o.name === name)
  return hit ? hit.id : null
}

/**
 * 把粘贴解析结果**写入**表单（原地改 `form`，返回被写入的字段名，供「来自粘贴」标注）。三个刻意的选择：
 * ① **只写解析出来的项**——没解析出来的字段保持原样，绝不置空（客服可能已经手填过）；② FK 按**精确同名**找，找不到就**不填**并把名字回给调用方（`unresolved`），由页面提示「没找到名称为 X 的区服，请手动选择」——静默不填会让客服以为解析器没认出来；③ 金额与文本都是**字符串**，全程不做浮点转换。
 */
export function applyParsedToForm(
  form: OrderForm,
  fields: ParsedFields,
  dicts: OptionDicts,
): { filled: string[]; unresolved: string[] } {
  const { scalars, byName } = pasteFillTargets(fields)
  const filled: string[] = []
  const unresolved: string[] = []

  for (const [key, value] of Object.entries(scalars)) {
    if (value === undefined) continue
    ;(form as unknown as Record<string, string>)[key] = value
    filled.push(key)
  }

  if (byName.game !== undefined) {
    const id = findByExactName(dicts.games, byName.game)
    if (id === null) {
      unresolved.push(`游戏「${byName.game}」`)
    } else {
      form.game = id
      filled.push('game')
    }
  }
  if (byName.server !== undefined) {
    const id = findByExactName(dicts.servers, byName.server)
    if (id === null) {
      unresolved.push(`区服「${byName.server}」`)
    } else {
      form.server = id
      filled.push('server')
    }
  }
  // T8：来源与游戏/区服**同形**（精确同名找 id；找不到 ⇒ 不静默）。
  // ⚠️ 只写 `order_source`（FK id），**不写** `source` 名字——后端按 id 覆盖写名字快照（spec §5.1）。
  if (byName.orderSource !== undefined) {
    const id = findByExactName(dicts.orderSources, byName.orderSource)
    if (id === null) {
      unresolved.push(`订单来源「${byName.orderSource}」`)
    } else {
      form.order_source = id
      filled.push('order_source')
    }
  }

  return { filled, unresolved }
}

/**
 * 表单 → 提交载荷：**空值不发送**（空串 / `null` 一律去掉；`is_urgent` 是布尔，`false` 也要发）。
 * `project_text` 与 `project` 同时存在时**以 `project_text` 为准**——两个都送、由后端按 spec 规则取值，**不要**在前端把 `project` 抹掉（客服可能正是想「先选上配置、再改文字」）。金额允许为空、也允许为负：必填闸在 `layui-island/pages/order-addorder.ts::totalPriceRequiredError`、**只在新建态**接（订单总价必填见 D-0082；本函数被别的页面复用，空值键的丢弃仍是通用规则，行为不动）。
 */
export function formToPayload(form: OrderForm, actor: string): CreateOrderPayload {
  const payload: Record<string, unknown> = { actor }
  for (const [key, value] of Object.entries(form)) {
    if (value === null || value === undefined) continue
    // 这里用 JS `trim()` 是因为它只回答「这个值值不值得发出去」（纯噪音不发），
    // **不是**「后端会不会接受」的判据——必填判据在 `@/config/money` 的 `isBlankLikePython`。
    if (typeof value === 'string' && value.trim() === '') continue
    payload[key] = value
  }
  return payload as CreateOrderPayload
}

/**
 * 操作人：取自**当前登录会话**（`stores/session.ts` 的 `user` 由 `me` / `login` 落地）。`display_name` 为空串时回退 `username`；只有拿不到登录态（`user === null`）才用占位符。三档行为都有测试（`__tests__/orderFormFields.spec.ts` 的「操作人取自登录态」）。
 * ⚠️ 别写反：后端**认证**已落地（`DEFAULT_PERMISSION_CLASSES = IsAuthenticated`），但接口层**尚未按权限过滤**——本函数只是把界面上显示与提交的操作人换成真实用户名，不构成访问控制。
 */
// 占位符只说「未登录」，不自我解释「前端还没有登录态」（登录态已接入，那句话是假的）；曾与 `ACTOR_TEMP_NOTE` 成对，后者已随其消费者一并删除——留一个会说假话的常量比删掉它更糟（`AGENTS.md` §4）。
export const ACTOR_PLACEHOLDER = '未登录'

export function currentActor(): string {
  const store = useSessionStore()
  const u = store.user
  return u ? u.display_name || u.username : ACTOR_PLACEHOLDER
}
