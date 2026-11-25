// @vitest-environment jsdom
import { existsSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { XgjShopBindingRow, XgjShopRow } from '@/api/xgjBindings'

/**
 * 【第三方平台 → 闲管家店铺对接】island 页（`pages/thirdparty-xgjshop.ts`）的契约测试。
 *
 * 这一页是**自有功能**（对标物里没有），所以没有证据件可对拍；下面钉的是**实现里几条
 * 有真实后果的约定**，不是"跑一遍不报错"：
 *
 * 1. **四步图文引导**（R9）与 **「铂金版 / ERP 专业版」那句声明**（R10）：纯文本断言，
 *    防的是"顺手删掉一句声明"——删掉之后页面照样渲染、没有任何报错；
 * 2. **两张引导图真在磁盘上**（不是只写了个 `src`）：路径写错时 `<img>` 只是空白，页面不报错；
 * 3. **应用表没有「对应订单来源」列、店铺表有那几列**（per-source 的结构性改动）；
 * 4. **密钥只写不读**（spec §9-S1）：弹层密码框**永远为空**，表格只渲染「已配置 / 未配置」；
 * 5. **弹层节点被搬出 `<form>` 之后，保存仍要读到用户输入**（layui 的 `data.field` 那时是空的，
 *    见实现文件头的坑 1）——这条测试**真的把节点搬进 `.layui-layer-content`** 再触发保存；
 * 6. **「同步店铺」真的调接口、真的刷表**（按钮挂在 layui 模板上，点错事件名是静默 no-op）；
 * 7. **同步失败必须说出来**（后端有意让"保存成功但同步失败"成为合法返回）；
 * 8. **订购异常标黄**，且文案里没有把标黄说成版本判定（D11）；
 * 9. **两列推送地址的注记按实现分开写**（订单推送会改单、商品推送只做接收确认，见
 *    `docs/specs/2026-10-04-xgj-push-inbound-design.md`）：旧文案那两句（「接收端尚未启用 /
 *    推送消息本轮不会入库」）2026-10-04 起**是假话**，⛔ 不许写回去。
 *
 * 手法照 `system-pddshipsetting.spec.ts`：`vi.mock` 掉 `bootstrapIsland`（jsdom 里加载不了真 layui）
 * 与 api 模块，只断言入参 / DOM。⚠️ `vi.mock` 会被提升，`import` 写在它之后。
 */

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** `src/frontend`（本文件位于 `src/frontend/src/layui-island/pages/__tests__/`）。 */
const FRONTEND_ROOT = path.resolve(HERE, '../../../..')

/** layui 的**可控替身**。`layer.open` 会像真 layui 一样把节点搬进 `.layui-layer-content`。 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const tableHandlers = new Map<string, (obj: unknown) => unknown>()
  const formHandlers = new Map<string, () => unknown>()
  const tableRenders: Array<Record<string, unknown>> = []
  const reloads: Array<{ id: string; data: unknown }> = []
  const opens: Array<Record<string, unknown>> = []
  const confirms: string[] = []
  return {
    msg,
    tableHandlers,
    formHandlers,
    tableRenders,
    reloads,
    opens,
    confirms,
    form: {
      render: vi.fn(),
      on: vi.fn((event: string, cb: () => unknown) => {
        formHandlers.set(event, cb)
      }),
    },
    table: {
      render: vi.fn((opts: Record<string, unknown>) => {
        tableRenders.push(opts)
      }),
      reloadData: vi.fn((id: string, opts: { data: unknown }) => {
        reloads.push({ id, data: opts.data })
      }),
      on: vi.fn((event: string, cb: (obj: unknown) => unknown) => {
        tableHandlers.set(event, cb)
      }),
    },
    layer: {
      /** 真 layui 会把 `content` 指向的节点**搬进** `.layui-layer-content`（本页的核心坑）。 */
      open: vi.fn((opts: Record<string, unknown>) => {
        opens.push(opts)
        const node = opts.content as HTMLElement | undefined
        if (node instanceof HTMLElement) {
          let host = document.querySelector('.layui-layer-content')
          if (!host) {
            host = document.createElement('div')
            host.className = 'layui-layer-content'
            document.body.appendChild(host)
          }
          host.appendChild(node)
        }
        return opens.length
      }),
      closeAll: vi.fn(),
      confirm: vi.fn((t: string, _o: unknown, ok: () => void) => {
        confirms.push(t)
        ok()
      }),
      alert: vi.fn(),
    },
    /** layui 的 jQuery 替身：本页只用它把节点交给 `layer.open`。 */
    $: (el: Element) => el,
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => ({
    form: LAYUI.form,
    table: LAYUI.table,
    layer: LAYUI.layer,
    $: LAYUI.$,
  })),
  initIslandElements: vi.fn(),
  // 真实现是 `islandMsg(layer, text)` → `layer.msg(text)`；替身只记文本。
  islandMsg: (_layer: unknown, text: string) => LAYUI.msg(text),
}))

vi.mock('@/api/xgjBindings', () => ({
  fetchXgjBindings: vi.fn(),
  createXgjBinding: vi.fn(),
  updateXgjBinding: vi.fn(),
  deleteXgjBinding: vi.fn(),
  testXgjConnection: vi.fn(),
  queryXgjOrderDetail: vi.fn(),
  syncXgjShops: vi.fn(),
  fetchXgjShops: vi.fn(),
}))

import {
  createXgjBinding,
  deleteXgjBinding,
  fetchXgjBindings,
  fetchXgjShops,
  queryXgjOrderDetail,
  syncXgjShops,
  testXgjConnection,
  updateXgjBinding,
  type XgjOrderDetail,
} from '@/api/xgjBindings'
import page, {
  PANEL_SELECTOR,
  SHOP_TABLE_ELEM,
  SHOP_TABLE_ID,
  TABLE_ELEM,
  TABLE_ID,
  DETAIL_LABELS,
  __testables,
  renderDetailRows,
  detailRowsToHtml,
  saveMessage,
  subscriptionCell,
  GUIDE_IMAGES,
} from '../thirdparty-xgjshop'

const { buildHtml } = __testables

/** 一行对接（**已配置密钥**，用来验证"配了也不回显"）。 */
const ROW: XgjShopBindingRow = {
  id: 7,
  shop_name: '主应用',
  app_key: 'AK-1',
  app_secret_set: true,
  order_push_url: 'http://tenant.test.com/api/business/xgj/push/order/',
  product_push_url: 'http://tenant.test.com/api/business/xgj/push/product/',
  remark: '备注',
}

/** 一家订购正常的店。 */
const SHOP_OK: XgjShopRow = {
  id: 11,
  shop_name: '甲店',
  user_name: 'member-a',
  order_source: 3,
  order_source_name: '甲店',
  is_valid: true,
  valid_end_time: 4102444800,
  subscription_warning: false,
  binding: 7,
  binding_name: '主应用',
}

/** 一家**订购异常**的店（`is_valid=false` / 订购过期）。 */
const SHOP_WARN: XgjShopRow = {
  ...SHOP_OK,
  id: 12,
  shop_name: '乙店',
  user_name: 'member-b',
  order_source: null,
  order_source_name: '',
  is_valid: false,
  valid_end_time: 0,
  subscription_warning: true,
}

function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

/** 取某个 elem 上那一次 `table.render` 的配置。 */
function renderOpts(elem: string): Record<string, unknown> {
  const found = LAYUI.tableRenders.find((r) => r.elem === elem)
  if (!found) throw new Error(`没有对 ${elem} 调用过 table.render`)
  return found
}

describe('闲管家店铺对接 island 页', () => {
  let root: HTMLElement

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.tableHandlers.clear()
    LAYUI.formHandlers.clear()
    LAYUI.tableRenders.length = 0
    LAYUI.reloads.length = 0
    LAYUI.opens.length = 0
    LAYUI.confirms.length = 0
    document.body.innerHTML = ''
    root = document.createElement('div')
    document.body.appendChild(root)
    vi.mocked(fetchXgjBindings).mockResolvedValue({
      items: [ROW],
      total: 1,
      page: 1,
      page_size: 200,
    })
    vi.mocked(fetchXgjShops).mockResolvedValue({ items: [SHOP_OK, SHOP_WARN] })
    vi.mocked(createXgjBinding).mockResolvedValue(ROW)
    vi.mocked(updateXgjBinding).mockResolvedValue(ROW)
    vi.mocked(deleteXgjBinding).mockResolvedValue(null)
    vi.mocked(testXgjConnection).mockResolvedValue({ shops: [] })
    vi.mocked(queryXgjOrderDetail).mockResolvedValue({ detail: { order_no: '123' } })
    vi.mocked(syncXgjShops).mockResolvedValue({
      total: 2,
      created_shops: 2,
      updated_shops: 0,
      created_sources: 2,
    })
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  async function mount(): Promise<void> {
    // 本页不用 `onNavigate`（没有跨页跳转）⇒ 按实现声明的签名只传 root
    await page.mount(root)
    await flush()
  }

  // ── R9：四步图文引导 ────────────────────────────────────────

  it('标记里写明四步引导，且菜单名与截图一致（「开放平台」而不是口述的「开发平台」）', () => {
    const html = buildHtml()
    // 四步的关键动作逐个钉住（少一步，租户就照不全）
    for (const text of [
      '开放平台',
      '添加应用',
      '我有自研系统',
      '进销存',
      'AppKey',
      'AppSecret',
      '确认上线',
      '同步店铺',
    ]) {
      expect(html, `引导里缺「${text}」`).toContain(text)
    }
    // ⚠️ 反向钉：截图里那个菜单叫「开放平台」。写成口述的「开发平台」会让租户在界面上找不到入口。
    expect(html).not.toContain('开发平台')
  })

  it('两张引导图**真在磁盘上**，且标记里引用的就是它们', () => {
    const html = buildHtml()
    // 从标记里真的把 src 抠出来核（只断言字符串常量会漏掉"路径写错"）
    const srcs = Array.from(html.matchAll(/src="([^"]*guide\/[^"]+)"/g)).map((m) => m[1]!)
    expect(srcs).toHaveLength(2)
    for (const src of srcs) {
      const rel = src.replace(/^\//, '')
      const abs = path.resolve(FRONTEND_ROOT, 'public', rel)
      expect(existsSync(abs), `${src} 指向的文件不在磁盘上（页面只会显示一块空白）`).toBe(true)
      expect(statSync(abs).size, `${src} 是空文件`).toBeGreaterThan(1000)
    }
    // 前置哨兵：两张图是**不同**的文件（复制同一张两次也算"两张都在"）
    expect(new Set(srcs).size).toBe(2)
    expect(GUIDE_IMAGES).toHaveLength(2)
  })

  // ── R10：版本声明（只声明 + 提醒）────────────────────────────

  it('R10：标记里声明「店铺须为铂金版或 ERP 专业版才能识别订单」', () => {
    // ⚠️ 这条是业务方**最后追加**的要求；一句话被顺手删掉时页面照样渲染、不报任何错，
    //    所以只能用文本断言钉住。
    const html = buildHtml()
    expect(html).toContain('铂金版')
    expect(html).toContain('ERP 专业版')
    expect(html).toContain('才能识别订单')
    // ⚠️ 「声明 + 提醒」的另一半：必须写明**标黄不是版本判定**，否则租户会把黄条读成"我不是铂金版"
    expect(html).toContain('标黄 ≠ 不是铂金版')
  })

  it('推送接收的口径（按实现写）与「一个来源只绑一边」两条提示仍在', () => {
    const html = buildHtml()
    // 接收端已建（`docs/specs/2026-10-04-xgj-push-inbound-design.md`）：订单推送只在
    // 「已退款 / 已关闭」时按来源单号把本系统的单改成「已退单」并写订单日志；商品推送只做接收确认。
    expect(html).toContain('推送接收已启用')
    expect(html).toContain('已退款')
    expect(html).toContain('已关闭')
    expect(html).toContain('来源单号')
    expect(html).toContain('已退单')
    // ⚠️ F6：一次成功退单写的是**两条**订单日志（①状态事实 ②带原因的退单，spec §D5）
    //    ⇒ 文案⛔ 不许写回「一条订单日志」（那是与实现不符的假话）。
    expect(html).toContain('写订单日志（状态变更 + 退单原因）')
    expect(html).not.toContain('一条订单日志')
    // ⚠️ 已结账的单**不改状态**，只有一条留给人工的日志（实现里是独立的那个动作码）
    expect(html).toContain('已结账')
    expect(html).toContain('留给人工处理')
    expect(html).toContain('商品推送只做接收确认，不改任何数据')
    // ⛔ 反向钉：旧文案那两句现在是**假话**（接收端已建，订单推送真的会入库）
    expect(html).not.toContain('尚未启用')
    expect(html).not.toContain('不会入库')
    // ⚠️ D6 的静默失效在用户侧**只有这一条**缓解（独立评审判定过是 Blocking）
    expect(html).toContain('一个订单来源只绑一边')
    expect(html).toContain('录单会走阿奇索')
    expect(html).toContain('不会生效')
  })

  // ── 两张表的结构 ────────────────────────────────────────────

  it('应用表**去掉**「对应订单来源」列（来源归店铺），店铺表有那五列', async () => {
    await mount()
    const appTitles = (renderOpts(TABLE_ELEM).cols as Array<Array<{ title?: string }>>)[0]!.map(
      (c) => c.title,
    )
    expect(appTitles).not.toContain('对应订单来源')
    expect(appTitles).toContain('AppSecret')
    expect(appTitles).toContain('订单消息推送URL')
    expect(appTitles).toContain('商品消息推送URL')

    const shopTitles = (
      renderOpts(SHOP_TABLE_ELEM).cols as Array<Array<{ title?: string }>>
    )[0]!.map((c) => c.title)
    expect(shopTitles).toEqual(
      expect.arrayContaining(['店铺名称', '会员名', '对应来源', '所属应用', '订购状态']),
    )
  })

  it('店铺表的「对应来源 / 所属应用 / 订购状态」渲染正确，订购异常**标黄**', async () => {
    await mount()
    const cols = (
      renderOpts(SHOP_TABLE_ELEM).cols as Array<Array<{ templet?: (d: XgjShopRow) => string }>>
    )[0]!
    const cells = (row: XgjShopRow) =>
      cols
        .filter((c) => typeof c.templet === 'function')
        .map((c) => c.templet!(row))
        .join('|')

    expect(cells(SHOP_OK)).toContain('甲店') // 对应来源 = 来源名
    expect(cells(SHOP_OK)).toContain('正常')
    // 订购异常：打上黄底（`#FFF3CD`）并写清后果
    expect(cells(SHOP_WARN)).toContain('FFF3CD')
    expect(cells(SHOP_WARN)).toContain('订购异常')
    expect(cells(SHOP_WARN)).toContain('可能识别不到订单')
    // 没来源的那家店要提示去点同步，而不是显示空白（空白会被当成"这家店没来源也没关系"）
    expect(cells(SHOP_WARN)).toContain('同步店铺')
  })

  it('店铺表的数据来自**按应用逐个取再拼**（两个应用都要请求到）', async () => {
    const second = { ...ROW, id: 8, shop_name: '第二应用' }
    vi.mocked(fetchXgjBindings).mockResolvedValue({
      items: [ROW, second],
      total: 2,
      page: 1,
      page_size: 200,
    })
    vi.mocked(fetchXgjShops).mockImplementation(async (id: number) => ({
      items: id === 7 ? [SHOP_OK] : [{ ...SHOP_OK, id: 99, shop_name: '第二家店' }],
    }))
    await mount()
    expect(vi.mocked(fetchXgjShops).mock.calls.map((c) => c[0])).toEqual([7, 8])
    const last = LAYUI.reloads.filter((r) => r.id === SHOP_TABLE_ID).pop()
    expect((last!.data as XgjShopRow[]).map((s) => s.shop_name)).toEqual(['甲店', '第二家店'])
  })

  // ── 密钥只写不读 ────────────────────────────────────────────

  it('弹层密码框是 password 且**永远为空**（后端不回密钥，页面上也没得填）', async () => {
    await mount()
    const panel = root.querySelector<HTMLElement>(PANEL_SELECTOR)!
    const secret = panel.querySelector<HTMLInputElement>('[name="app_secret"]')!
    expect(secret.type).toBe('password')
    expect(secret.value).toBe('')
    expect(secret.placeholder).toBe('留空表示不修改')

    const tool = LAYUI.tableHandlers.get(`tool(${TABLE_ID})`)!
    tool({ data: ROW, event: 'edit' })
    expect(panel.querySelector<HTMLInputElement>('[name="shop_name"]')!.value).toBe('主应用')
    expect(panel.querySelector<HTMLInputElement>('[name="app_key"]')!.value).toBe('AK-1')
    expect(panel.querySelector<HTMLInputElement>('[name="app_secret"]')!.value).toBe('')
  })

  it('应用表渲染的是「已配置 / 未配置」，**不是**密钥本身；两列推送地址各写**各自**的后果', async () => {
    await mount()
    const cols = (
      renderOpts(TABLE_ELEM).cols as Array<
        Array<{ templet?: (d: XgjShopBindingRow) => string }>
      >
    )[0]!
    const rendered = cols
      .filter((c) => typeof c.templet === 'function')
      .map((c) => c.templet!(ROW))
      .join('')
    expect(rendered).toContain('已配置')
    expect(rendered).not.toContain('SK-')
    // ⚠️ 两列的注记**不一样**：订单推送会改单、商品推送只做接收确认（⛔ 合成一句「都会入库」就是假话）
    expect(rendered).toContain('已启用：按来源单号把已退款 / 已关闭的单改成已退单并写订单日志（状态变更 + 退单原因）')
    expect(rendered).toContain('已启用：只做接收确认，不改任何数据')
    // ⛔ 反向钉：旧文案那句「（接收端尚未启用）」2026-10-04 起不成立
    expect(rendered).not.toContain('尚未启用')
  })

  // ── E-0303 / F-01：删除确认的插值是 HTML 上下文（备注名 / AppKey 是自由文本）──

  it('E-0303：删除确认里备注名含 HTML 载荷 ⇒ 上屏的是转义文本，载荷原样不出现', async () => {
    await mount()
    const XSS_NAME = '<img src="x" onerror=window.__xss=1>'
    const XSS_ESCAPED = '&lt;img src=&quot;x&quot; onerror=window.__xss=1&gt;'
    const tool = LAYUI.tableHandlers.get(`tool(${TABLE_ID})`)!
    tool({ data: { ...ROW, shop_name: XSS_NAME }, event: 'del' })
    expect(LAYUI.confirms).toHaveLength(1)
    expect(LAYUI.confirms[0]).toContain(`确定删除「${XSS_ESCAPED}」这条对接吗？`)
    expect(LAYUI.confirms[0]).not.toContain(XSS_NAME)
    expect(LAYUI.confirms[0]).not.toContain('<img')
  })

  it('E-0303：备注名回落到 AppKey 时（shop_name 空）同样转义', async () => {
    await mount()
    const tool = LAYUI.tableHandlers.get(`tool(${TABLE_ID})`)!
    tool({ data: { ...ROW, shop_name: '', app_key: 'AK<img>' }, event: 'del' })
    expect(LAYUI.confirms[0]).toContain('确定删除「AK&lt;img&gt;」这条对接吗？')
    expect(LAYUI.confirms[0]).not.toContain('AK<img>')
  })

  // ── 保存 ────────────────────────────────────────────────────

  it('节点被搬进弹层之后，保存仍读到用户输入；载荷里**没有** order_source', async () => {
    await mount()
    const panel = root.querySelector<HTMLElement>(PANEL_SELECTOR)!

    // ① 打开弹层 ⇒ 节点被搬进 `.layui-layer-content`（替身照真 layui 的行为做）
    const tool = LAYUI.tableHandlers.get(`tool(${TABLE_ID})`)!
    tool({ data: ROW, event: 'edit' })
    expect(document.querySelector(`.layui-layer-content > ${PANEL_SELECTOR}`)).not.toBeNull()
    expect(root.querySelector(PANEL_SELECTOR)).toBeNull()

    // ② 在弹层里改值（只改备注、密钥留空）
    panel.querySelector<HTMLInputElement>('[name="remark"]')!.value = '改过的备注'
    panel.querySelector<HTMLInputElement>('[name="app_secret"]')!.value = ''

    // ③ 触发保存
    LAYUI.formHandlers.get('submit(xgjSave)')!()
    await flush()

    expect(updateXgjBinding).toHaveBeenCalledTimes(1)
    expect(vi.mocked(updateXgjBinding).mock.calls[0]![0]).toBe(7)
    // ⚠️ 空串照传：后端把空串当「不改」（spec §9-S4）；传成别的语义就是静默清密钥
    // ⚠️ 载荷里**不该**再有 `order_source`（per-source spec D3：来源归店铺）
    expect(vi.mocked(updateXgjBinding).mock.calls[0]![1]).toEqual({
      shop_name: '主应用',
      app_key: 'AK-1',
      app_secret: '',
      remark: '改过的备注',
    })
  })

  it('保存后把**同步结果**说出来（同步失败是真会发生的合法返回）', async () => {
    vi.mocked(updateXgjBinding).mockResolvedValue({
      ...ROW,
      sync: { error: '店铺同步失败', code: 1016 },
    })
    await mount()
    LAYUI.tableHandlers.get(`tool(${TABLE_ID})`)!({ data: ROW, event: 'edit' })
    LAYUI.formHandlers.get('submit(xgjSave)')!()
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith(expect.stringContaining('已保存，但店铺同步失败'))
  })

  it('纯函数 `saveMessage`：四种同步结果各说各话，且都不说"没问题"', () => {
    expect(saveMessage(undefined)).toBe('已保存')
    expect(saveMessage({ skipped: 'AppKey / AppSecret 还没填完整' })).toContain('还没填完整')
    expect(saveMessage({ error: '店铺同步失败', code: 1017 })).toContain('错误码 1017')
    // 成功那一支要把计数讲清楚（"已保存"两个字让人以为店铺也同步好了）
    const ok = saveMessage({ created_shops: 2, updated_shops: 1, created_sources: 2 })
    expect(ok).toContain('2 家新店铺')
    expect(ok).toContain('新建 2 个订单来源')
  })

  // ── 同步店铺 ────────────────────────────────────────────────

  it('「同步店铺」调接口并刷新店铺表（事件名挂在 layui 模板上，点错是静默 no-op）', async () => {
    await mount()
    LAYUI.reloads.length = 0
    LAYUI.tableHandlers.get(`tool(${TABLE_ID})`)!({ data: ROW, event: 'sync' })
    await flush()
    expect(syncXgjShops).toHaveBeenCalledWith(7)
    // 承重判据：**店铺表真的重载了**（只弹一句"已同步"而不刷表 = 租户看到的还是旧数据）
    expect(LAYUI.reloads.map((r) => r.id)).toContain(SHOP_TABLE_ID)
    expect(LAYUI.msg).toHaveBeenCalledWith(expect.stringContaining('新建来源 2 个'))
  })

  it('同步失败时把业务错误说出来，不静默', async () => {
    const { BizError } = await import('@/api/errors')
    vi.mocked(syncXgjShops).mockRejectedValue(new BizError(1016, '上游凭据无效'))
    await mount()
    LAYUI.tableHandlers.get(`tool(${TABLE_ID})`)!({ data: ROW, event: 'sync' })
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith('上游凭据无效')
  })

  // ── 订购状态的纯函数 ────────────────────────────────────────

  it('纯函数 `subscriptionCell`：黄条只讲订购，不讲版本；**没数据不装作"正常"**', () => {
    expect(subscriptionCell(SHOP_WARN)).toContain('FFF3CD')
    expect(subscriptionCell(SHOP_WARN)).toContain('订购异常')
    // ⚠️ 不许把标黄说成版本判定（D11：上游不返回版本名，硬猜就是编）
    expect(subscriptionCell(SHOP_WARN)).not.toContain('版本')
    expect(subscriptionCell(SHOP_OK)).not.toContain('FFF3CD')
    // ⚠️ 独立评审的 Minor：后端对 `null` **不标黄**是对的（"没数据" ≠ "异常"），
    //    但前端把"没数据"渲染成绿色「正常」是一句**没有依据的断言**（页面替上游担保了
    //    一件它其实不知道的事，E-0011 同族）⇒ 必须显示「—」。
    const unknown: XgjShopRow = { ...SHOP_OK, is_valid: null, valid_end_time: null }
    expect(subscriptionCell(unknown)).toContain('—')
    expect(subscriptionCell(unknown)).not.toContain('正常')
  })

  // ── 查详情 ──────────────────────────────────────────────────

  it('查详情：结果区渲染中文标签，且**转义**第三方文本', async () => {
    const detail: XgjOrderDetail = {
      order_no: '123',
      total_amount: 8000,
      buyer_nick: '<img src=x onerror=alert(1)>',
      we_dont_know_this: '未登记字段也要显示',
      goods: { title: '代练', price: 8000 },
    }
    vi.mocked(queryXgjOrderDetail).mockResolvedValue({ detail })
    await mount()

    LAYUI.tableHandlers.get(`toolbar(${TABLE_ID})`)!({ event: 'query' })
    const panel = document.querySelector<HTMLElement>('.layui-layer-content > .xgjDetailDiv')!
    panel.querySelector<HTMLInputElement>('[name="order_no"]')!.value = '123'
    LAYUI.formHandlers.get('submit(xgjQuery)')!()
    await flush()

    const result = panel.querySelector('.xgjDetailResult')!
    expect(result.innerHTML).toContain('闲鱼订单号')
    expect(result.innerHTML).toContain('订单下单金额（分）')
    expect(result.innerHTML).toContain('商品标题')
    // 未登记字段**照样显示**（标签回落到英文原名）
    expect(result.innerHTML).toContain('we_dont_know_this')
    // 第三方文本转义
    expect(result.innerHTML).not.toContain('<img src=x')
    expect(result.innerHTML).toContain('&lt;img')
  })

  it('查详情：订单号不是纯数字时不发请求（后端也会挡，但白跑一趟没意义）', async () => {
    await mount()
    LAYUI.tableHandlers.get(`toolbar(${TABLE_ID})`)!({ event: 'query' })
    const panel = document.querySelector<HTMLElement>('.layui-layer-content > .xgjDetailDiv')!
    panel.querySelector<HTMLInputElement>('[name="order_no"]')!.value = 'abc'
    LAYUI.formHandlers.get('submit(xgjQuery)')!()
    await flush()
    expect(queryXgjOrderDetail).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('订单号只能是数字')
  })

  it('纯函数：详情展平（含 goods 前缀与 null）', () => {
    expect(
      renderDetailRows({ order_no: '1', nothing: null, goods: { title: 'T', price: 0 } }),
    ).toEqual([
      [DETAIL_LABELS.order_no, '1'],
      ['nothing', ''],
      ['商品标题', 'T'],
      ['商品单价（分）', '0'],
    ])
    // 标签表里不能出现重复的中文标签（重复会让两行看起来一模一样）
    const labels = Object.values(DETAIL_LABELS)
    expect(new Set(labels).size).toBe(labels.length)
  })

  it('纯函数：`detailRowsToHtml` 转义标签与值', () => {
    const html = detailRowsToHtml([['<b>标签</b>', '<script>x</script>']])
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('<b>标签')
    expect(html).toContain('&lt;script&gt;')
  })
})
