import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { User } from '@/api/auth'
import { MONEY_FIELD_LABELS, ORDER_MONEY_FIELDS } from '@/config/money'
import { useSessionStore } from '@/stores/session'
import {
  ACTOR_PLACEHOLDER,
  FIELD_LABELS,
  ORDER_FORM_GROUPS,
  applyParsedToForm,
  currentActor,
  emptyOrderForm,
  fieldLabel,
  formToPayload,
  pasteFillTargets,
} from '../orderFormFields'

const DICTS = {
  games: [{ id: 1, name: '原神' }],
  servers: [{ id: 7, name: '国服' }],
  categories: [],
  projects: [],
  // T8 新增的来源字典。夹具一律**合成值**（本仓 PII 真值守卫扫 `src/**`）。
  orderSources: [{ id: 31, name: '合成来源甲' }],
}

describe('粘贴解析结果 → 表单', () => {
  it('只写解析出来的项，没解析出来的字段保持原样', () => {
    const form = emptyOrderForm()
    form.customer_qq = '手填的'

    applyParsedToForm(form, { game_account: 'acc001', total_price: '-199.50' }, DICTS)

    expect(form.game_account).toBe('acc001')
    expect(form.total_price).toBe('-199.50')
    expect(form.customer_qq).toBe('手填的')
  })

  it('金额负号保留（D-0015）', () => {
    const form = emptyOrderForm()
    applyParsedToForm(form, { total_price: '-199.50' }, DICTS)
    expect(form.total_price).toBe('-199.50')
  })

  it('名称类字段按精确同名找 FK；找不到就不填并回报', () => {
    const form = emptyOrderForm()
    const result = applyParsedToForm(form, { game_name: '原神', server_name: '国际服' }, DICTS)

    expect(form.game).toBe(1)
    expect(form.server).toBe(null)
    expect(result.filled).toEqual(['game'])
    expect(result.unresolved).toEqual(['区服「国际服」'])
  })

  it('pasteFillTargets 把金额格式化成两位小数', () => {
    expect(pasteFillTargets({ total_price: '-199.5' })).toEqual({
      scalars: { total_price: '-199.50' },
      byName: {},
    })
  })

  /**
   * `/parse/` 会多回 5 个键（配置驱动的识别项产物）。判别力（哪一处生产改动会让哪条红）：
   * 四个 scalar 那条钉 `ParsedFields` 的键 + `pasteFillTargets` 的路由——少任何一个键 `filled` 就少一个；来源命中那条钉 `byName.orderSource` → 精确同名找 id 的整条路（`applyParsedToForm` 不处理它 ⇒ `order_source` 仍是 `null`）；来源未命中那条钉「不静默」——退回「找不到就跳过」的写法 ⇒ `unresolved` 为空、用例红。
   */
  it('T8：四个 scalar 键逐字落位（当前信息 1/2、旺旺、来源单号）', () => {
    const form = emptyOrderForm()
    const result = applyParsedToForm(
      form,
      {
        current_info_1: '合成当前信息一',
        current_info_2: '合成当前信息二',
        customer_ww: '合成旺旺昵称',
        source_order_no: 'SRC-0001',
      },
      DICTS,
    )

    expect(form.current_info_1).toBe('合成当前信息一')
    expect(form.current_info_2).toBe('合成当前信息二')
    expect(form.customer_ww).toBe('合成旺旺昵称')
    expect(form.source_order_no).toBe('SRC-0001')
    expect(result.filled).toEqual([
      'current_info_1',
      'current_info_2',
      'customer_ww',
      'source_order_no',
    ])
    expect(result.unresolved).toEqual([])
  })

  it('T8：pasteFillTargets 把 source 路由进 byName.orderSource（不是 scalars）', () => {
    const out = pasteFillTargets({ source: '合成来源甲', source_order_no: 'SRC-1' })
    expect(out.scalars).toEqual({ source_order_no: 'SRC-1' })
    expect(out.byName).toEqual({ orderSource: '合成来源甲' })
  })

  it('T8：来源名按**精确同名**在 orderSources 里找 id ⇒ 写 order_source（数字 id）', () => {
    const form = emptyOrderForm()
    const result = applyParsedToForm(form, { source: '合成来源甲' }, DICTS)

    expect(form.order_source).toBe(31)
    // 别被 `String()` 化：提交的是 FK 数字 id（E-0021 同族：别拿真值判）
    expect(typeof form.order_source).toBe('number')
    expect(result.filled).toEqual(['order_source'])
    expect(result.unresolved).toEqual([])
    // 名字本身**不**写进 `source`：后端按 id 覆盖写名字快照（spec §5.1）
    expect(form.source).toBe('')
  })

  it('T8：来源名对不上 ⇒ 进 unresolved（与游戏/区服同口径，不静默）、order_source 不变', () => {
    const form = emptyOrderForm()
    form.order_source = 99
    const result = applyParsedToForm(form, { source: '字典里没有的合成来源' }, DICTS)

    expect(form.order_source).toBe(99)
    expect(result.filled).toEqual([])
    expect(result.unresolved).toEqual(['订单来源「字典里没有的合成来源」'])
  })
})

// 原来这条 describe 叫「unmatched 中文化…」，但中文化+保序的那条用例随 `unmatchedLabels` 被删掉了
// （评审 Round 2 的 minor）；现在只剩 `fieldLabel` 的兜底一条，标题按它实际覆盖的范围收窄。
describe('未知字段名的兜底显示（fieldLabel，不显示 undefined）', () => {
  it('认不出来的名字原样显示，不显示 undefined', () => {
    expect(fieldLabel('brand_new_field')).toBe('brand_new_field')
  })

  it('认识的名字给中文', () => {
    expect(fieldLabel('game_sub_account')).toBe('子账号')
    expect(fieldLabel('customer_phone')).toBe('联系电话')
  })
})

describe('分组顺序逐字照 P2 spec §5.2', () => {
  it('六个分组与顺序', () => {
    expect(ORDER_FORM_GROUPS.map((g) => g.title)).toEqual([
      '订单内容',
      '基本',
      '玩家账号',
      '价格',
      '客户联系',
      '其他',
    ])
  })
})

describe('formToPayload：空值不发、负数原样、project_text 与 project 同时存在', () => {
  it('空串与 null 不发送，is_urgent:false 要发', () => {
    const form = emptyOrderForm()
    form.game = 3
    form.project_text = '深渊满星'
    form.total_price = '-199.50'

    expect(formToPayload(form, '客服小张')).toEqual({
      actor: '客服小张',
      game: 3,
      project_text: '深渊满星',
      total_price: '-199.50',
      is_urgent: false,
    })
  })

  it('project 与 project_text 同时存在时两个都送（后端以 project_text 为准）', () => {
    const form = emptyOrderForm()
    form.game = 3
    form.project = 9
    form.project_text = '客服手改的名字'

    expect(formToPayload(form, '客服小张')).toMatchObject({
      game: 3,
      project: 9,
      project_text: '客服手改的名字',
    })
  })
})

describe('订单来源（order_source：来源字典 id，spec §6.2）', () => {
  // 判别力（写测试时就点明「哪一处生产改动会让它红」）：第 1 条钉 `emptyOrderForm` 的**键与初值**——键不存在时读到的是 `undefined`（红）；
  // 第 2 条钉「数字 id 不被丢、不被 `String()` 化」——`formToPayload` 改成白名单就红；第 3 条钉「没选来源不发这个键」——把 `null` 也发出去就红（E-0021 的同族：空值判据）。
  it('emptyOrderForm 带 order_source 键，初值 null（不是 undefined）', () => {
    expect('order_source' in emptyOrderForm()).toBe(true)
    expect(emptyOrderForm().order_source).toBeNull()
  })

  it('选了来源 ⇒ 载荷含 order_source（数字原样，不被丢）', () => {
    const payload = formToPayload(
      { ...emptyOrderForm(), game: 1, project_text: 'x', order_source: 20 },
      'u',
    )
    expect(payload.order_source).toBe(20)
  })

  it('没选来源（null）⇒ 载荷里没有 order_source 这个键（空值不发）', () => {
    const payload = formToPayload({ ...emptyOrderForm(), game: 1, project_text: 'x' }, 'u')
    expect('order_source' in payload).toBe(false)
  })
})

describe('金额字段的中文名与 config/money.ts 同源', () => {
  // brief 说「这里有一条断言钉住 FIELD_LABELS 与 MONEY_FIELD_LABELS 相同」，但它给的用例里**没有**，
  // 所以补上：两份清单（页面显示名 / 金额格式层显示名）漂移时，只有这条会红。
  it('6 个金额字段逐字段同值', () => {
    expect(Object.fromEntries(ORDER_MONEY_FIELDS.map((f) => [f, FIELD_LABELS[f]]))).toEqual(
      MONEY_FIELD_LABELS,
    )
  })
})

describe('操作人取自登录态（P5 接线）', () => {
  // `currentActor()` → `useSessionStore()`，没有 active pinia 会直接抛。
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  const user = (extra: Partial<User> = {}): User => ({
    id: 1,
    username: 'kefu',
    display_name: '小客服',
    identity_plane: 'tenant_acme',
    is_tenant_admin: false,
    roles: [],
    permissions: [],
    ...extra,
  })

  it('占位文案是「未登录」，不再是「P2 临时」的自我说明', () => {
    expect(ACTOR_PLACEHOLDER).toBe('未登录')
  })

  it('未登录（store.user 为 null）→ 占位符', () => {
    expect(currentActor()).toBe(ACTOR_PLACEHOLDER)
  })

  it('已登录 → 取 display_name', () => {
    useSessionStore().user = user({ display_name: '小客服' })
    expect(currentActor()).toBe('小客服')
  })

  it('display_name 为空 → 回退 username', () => {
    useSessionStore().user = user({ display_name: '', username: 'kefu' })
    expect(currentActor()).toBe('kefu')
  })
})
