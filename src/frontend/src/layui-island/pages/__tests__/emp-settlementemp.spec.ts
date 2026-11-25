// @vitest-environment jsdom
/**
 * 【清算绩效】弹层子页（对标 `/Emp/SettlementEmp?ids=<逗号串>`）的测试。
 *
 * 本页是父页（`emp-emplist.ts`）用 `layer.open({type:2, content:'…?page=emp-settlementemp&ids=…'})`
 * 打开的**独立 iframe 页面**（对标证据件 `public/replica/emp-settlementemp.html` + 页面 JS
 * `.tools/recon/settlementemp/js/JS2019__Emp__settlementemp.js`，后者**不进 git**）。
 *
 * 三层覆盖：
 *  1. **结构**：标记与证据件逐块对齐（`blockquote` 原文、5 组 `.layui-form-item`、两个
 *     `.expdiv[style="display:none"]`、四格的红字/加粗**视觉值**、按钮 class 里的双空格、隐藏域）；
 *  2. **纯函数**：id 解析 / 金额与文案口径（`0元` vs `X.XX元`）/ 结果提示的两种形态 / 父页刷新的兜底；
 *  3. **驱动真交互**（`AGENTS.md` §5-G 要求新建 island 页必须有）：真挂载 + 真点 radio + 真跑
 *     `form.on` 回调，断言「点了没反应 / 请求发错」这类 `typecheck` 与 `lint` 都看不见的失败。
 *
 * ⚠️ 本文件用的是**假 layui**（不加载 3.9 MB 的 `public/layui/layui.js`）：`bootstrapIsland` 被 mock，
 * `form.on` 把回调记进 Map、由测试按本仓既有写法手动触发（同 `system-setgames.spec.ts`）。
 * 它能抓到「事件根本没注册」（Map 里没有那个 key ⇒ 断言红），抓不到真 layui 的渲染细节。
 *
 * ⚠️ **`window.parent` 必须被换成假窗口才测得动**：jsdom 里 `window.parent === window` ⇒「读本页
 * `window`」与「读 `window.parent`」两条实现**读数完全一样**，本页那条真机缺陷（结算成功却弹层不关、
 * 父页列表不刷）正是这样漏过单测的 ⇒ 见 `stubParentWindow` 及其 docstring。
 * ⚠️ `api/settlements` 整个模块被替身：本文件只钉「页面拿什么参数调它」，传输层（URL / 参数形态）
 * 由 `src/api/__tests__/settlements.spec.ts` 钉住。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/* ─────────────────────── 假 layui ─────────────────────── */

const LAYUI = vi.hoisted(() => {
  const formHandlers = new Map<string, (obj: unknown) => unknown>()
  const msg = vi.fn()
  const prompt = vi.fn()
  const close = vi.fn()
  const photos = vi.fn()
  const closeAll = vi.fn()
  const renders: string[] = []
  return {
    formHandlers,
    msg,
    prompt,
    close,
    photos,
    closeAll,
    renders,
    form: {
      render: vi.fn((type?: string) => {
        renders.push(type ?? '')
      }),
      on: vi.fn((event: string, cb: (obj: unknown) => unknown) => {
        formHandlers.set(event, cb)
      }),
    },
    layer: { msg, prompt, close, photos, closeAll },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => ({ form: LAYUI.form, layer: LAYUI.layer })),
  initIslandElements: vi.fn(),
}))

vi.mock('@/api/settlements', () => ({
  fetchSettlementPreview: vi.fn(),
  settlePerformances: vi.fn(),
  revokeSettlements: vi.fn(),
}))

import {
  fetchSettlementPreview,
  revokeSettlements,
  settlePerformances,
  type SettlementPreview,
} from '@/api/settlements'
import { initIslandElements } from '@/layui/island'
import page, {
  CELL_LABELS,
  MESSAGES,
  PAGE_KEY,
  PAGE_TITLE,
  QR_PHOTOS_TITLE,
  REASON_PROMPT_TITLE,
  countText,
  formatMoney,
  moneyText,
  ownersText,
  parentToolsWindow,
  parsePerformanceIds,
  qrCodesMarkup,
  reloadParentList,
  revokeResultText,
  settlementEmpFormHtml,
  settleResultText,
} from '../emp-settlementemp'

/** 一份合成预览载荷（**夹具一律用合成值**：真值只在 `.tools/recon/`，E-0077）。 */
const PREVIEW: SettlementPreview = {
  count: 2,
  temp_total: '20.00',
  bonus_total: '0.00',
  settled_count: 1,
  unsettled_count: 1,
  owners: [{ user_id: 3, username: '测试昵称甲', amount: '0.00' }],
  qr_codes: [
    { user_id: 3, username: '测试昵称甲', name: '测试姓名甲', url: '/media/qr/3.png?sig=abc' },
  ],
}

/** 零态（对标证据件抓的就是 `ids=0` 那一份：`0笔 / 0元 / 0元 / 空`）。 */
const EMPTY_PREVIEW: SettlementPreview = {
  count: 0,
  temp_total: '0.00',
  bonus_total: '0.00',
  settled_count: 0,
  unsettled_count: 0,
  owners: [],
  qr_codes: [],
}

function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

/**
 * 取某个 mock 的**最后一次**调用。⚠️ 不用 `.at(-1)`：本工程的 TS lib 没到 ES2022
 * （`vue-tsc` 会报 `Property 'at' does not exist`）。
 */
function lastCall(fn: { mock: { calls: unknown[][] } }): unknown[] | undefined {
  const calls = fn.mock.calls
  return calls.length === 0 ? undefined : calls[calls.length - 1]
}

/** 把 `ids` 写进 `window.location.search`（子页唯一的参数来源，island 的 context 不带 query）。 */
function setSearch(ids: string): void {
  window.history.replaceState(
    {},
    '',
    `/layui-island/page.html?page=${PAGE_KEY}&ids=${ids}`,
  )
}

describe('清算绩效子页：标记与证据件逐块对齐', () => {
  const doc = (ids: readonly number[] = [1, 2]): Document =>
    new DOMParser().parseFromString(
      `<div id="root">${settlementEmpFormHtml(ids)}</div>`,
      'text/html',
    )

  it('页内 `<style>`：`body{background-color:#fff}` + `.qrCodeBox` 两条的值逐字（证据件 L24-27 / L51-66）', () => {
    const css = doc().querySelector('style')!.textContent ?? ''
    expect(css).toContain('background-color: #fff')
    // `.qrCodeBox img`（L53-57）——四个值逐个钉住，不靠 toContain 一句含混过去。
    expect(css).toContain('width:98px;')
    expect(css).toContain('height:98px;')
    expect(css).toContain('margin-right:10px;')
    // `.qrCodeBox`（L58-65）：⚠️ `display :flex;` 冒号前那个空格是原文。
    expect(css).toContain('display :flex;')
    expect(css).toContain('text-align:center;')
    expect(css).toContain('flex-wrap: wrap;')
    expect(css).toContain('color: red;')
    expect(css).toContain('font-size: 14px;')
    expect(css).toContain('line-height: 20px;')
  })

  it('表单外壳：class / `action=""` / 内联 style 与证据件 L67 一致', () => {
    const form = doc().querySelector('form')!
    expect(form.getAttribute('class')).toBe('layui-form layui-form-pane')
    expect(form.getAttribute('action')).toBe('')
    expect(form.getAttribute('style')).toBe(
      'margin-left:10px;margin-right:10px;padding-bottom:20px',
    )
  })

  it('`blockquote` 提示语**逐字**照证据件 L68-70（连「注意：」那半句）', () => {
    const quote = doc().querySelector('blockquote')!
    expect(quote.getAttribute('class')).toBe('layui-elem-quote')
    expect(quote.textContent?.trim()).toBe(
      '你正在进行给代练打手或销售人员进行账单结算或修改结算状态，请注意所勾选的绩效记录是否有误！' +
        '注意：订单状态必须在待验收、已完成、已退单等时才可以改成已结算',
    )
  })

  it('内层 `.layui-form-pane` 的 `margin-*`（证据件 L71）', () => {
    const pane = doc().querySelector('form > .layui-form-pane')!
    expect(pane.getAttribute('style')).toBe(
      'margin-top:10px;margin-right:10px;margin-bottom:10px',
    )
  })

  it('5 组 `.layui-form-item` 的标签顺序（笔数 / 预计 / 实际 / 结算对象 / 结算状态）', () => {
    const pane = doc().querySelector('form > .layui-form-pane')!
    expect(
      Array.from(pane.querySelectorAll('.layui-form-item > .layui-form-label')).map((l) =>
        l.textContent?.trim(),
      ),
    ).toEqual([
      CELL_LABELS.count,
      CELL_LABELS.tempTotal,
      CELL_LABELS.bonusTotal,
      CELL_LABELS.owners,
      '结算状态',
      '同步到账本',
      '微信收款码',
    ])
  })

  it('四格的**视觉值**：`color:#ff0000`；后三格另加 `font-weight:600`；初值 `0笔` / `0元` / `0元` / 空', () => {
    const spans = Array.from(
      doc().querySelectorAll<HTMLElement>('.layui-form-item > .layui-input-block > span'),
    )
    expect(spans.length).toBe(4)
    expect(spans.map((s) => s.getAttribute('style'))).toEqual([
      'margin-left:10px;line-height:36px;color:#ff0000',
      'margin-left:10px;line-height:36px;color:#ff0000;font-weight:600',
      'margin-left:10px;line-height:36px;color:#ff0000;font-weight:600',
      'margin-left:10px;line-height:36px;color:#ff0000;font-weight:600',
    ])
    expect(spans.map((s) => s.textContent)).toEqual(['0笔', '0元', '0元', ''])
  })

  it('结算状态两个 radio：`name=sta`、`value=N/Y`、`title=未结算/已结算`、`lay-filter=sta`（证据件 L99-100）', () => {
    const radios = Array.from(doc().querySelectorAll<HTMLInputElement>('input[name="sta"]'))
    expect(radios.map((r) => r.value)).toEqual(['N', 'Y'])
    expect(radios.map((r) => r.getAttribute('title'))).toEqual(['未结算', '已结算'])
    expect(radios.map((r) => r.getAttribute('lay-filter'))).toEqual(['sta', 'sta'])
    // 两个都**不预选**（对标原文没有 checked）⇒ 未选状态那条路径真实可达。
    expect(radios.some((r) => r.hasAttribute('checked'))).toBe(false)
  })

  it('两个 `.expdiv` 初始 `display:none`；「同步到账本」默认勾「不需要」（证据件 L103-116）', () => {
    const divs = Array.from(doc().querySelectorAll<HTMLElement>('.expdiv'))
    expect(divs.length).toBe(2)
    expect(divs.map((d) => d.getAttribute('style'))).toEqual(['display:none', 'display:none'])
    expect(divs.map((d) => d.querySelector('.layui-form-label')?.textContent?.trim())).toEqual([
      '同步到账本',
      '微信收款码',
    ])
    const sync = Array.from(doc().querySelectorAll<HTMLInputElement>('input[name="syncexp"]'))
    expect(sync.map((r) => r.value)).toEqual(['N', 'Y'])
    expect(sync.map((r) => r.getAttribute('title'))).toEqual([
      '不需要',
      '需要（将会在财务运营/日常账本里新增一笔支出款的记录）',
    ])
    expect(sync[0]!.hasAttribute('checked')).toBe(true)
    expect(sync[1]!.hasAttribute('checked')).toBe(false)
    // 收款码容器在第二个 `.expdiv` 里，初始为空（对标由 JS 填）。
    expect(divs[1]!.querySelector('.qrCodeBox')?.innerHTML).toBe('')
  })

  it('居中的「确定结算或修改」：class 里**两个空格**、`lay-submit="" lay-filter="savebtn"`（证据件 L119-121）', () => {
    const holder = doc().querySelector('form > div[style="text-align:center;align-items:center"]')!
    const btn = holder.querySelector('button')!
    expect(btn.getAttribute('class')).toBe('layui-btn  layui-btn-normal w-lg')
    expect(btn.getAttribute('type')).toBe('button')
    expect(btn.getAttribute('lay-submit')).toBe('')
    expect(btn.getAttribute('lay-filter')).toBe('savebtn')
    expect(btn.textContent).toBe('确定结算或修改')
  })

  it('隐藏域 `#empIds` 收下逗号串（提交时读的就是它，对标 JS L63）', () => {
    const hidden = doc([126508, 126394]).querySelector<HTMLInputElement>('#empIds')!
    expect(hidden.getAttribute('type')).toBe('hidden')
    expect(hidden.getAttribute('name')).toBe('empIds')
    expect(hidden.value).toBe('126508,126394')
  })

  it('QR 一项的标记逐字照页面 JS L38-44（`height="72"`、`data-index`、两行名字）', () => {
    const html = qrCodesMarkup(PREVIEW.qr_codes)
    const d = new DOMParser().parseFromString(`<div>${html}</div>`, 'text/html')
    const img = d.querySelector('img.qrCodeImg')!
    expect(img.getAttribute('height')).toBe('72')
    expect(img.getAttribute('data-index')).toBe('0')
    expect(img.getAttribute('src')).toBe('/media/qr/3.png?sig=abc')
    expect(d.querySelector('.qrCodeUserName')!.textContent).toBe('测试昵称甲')
    expect(d.querySelector('.qrCodeName')!.textContent).toBe('测试姓名甲')
    expect(d.querySelector('.qrCodeItem')).toBeTruthy()
  })

  it('QR 里的用户名 / 姓名 / URL 过 `escapeHtml`（租户数据是存储型 XSS 的入口）', () => {
    const html = qrCodesMarkup([
      { user_id: 1, username: '<img onerror=x>', name: 'a"b', url: '/x?a=1&b=2' },
    ])
    expect(html).toContain('&lt;img onerror=x&gt;')
    expect(html).toContain('a&quot;b')
    expect(html).toContain('/x?a=1&amp;b=2')
    expect(html).not.toContain('<img onerror=x>')
  })
})

describe('清算绩效子页：纯函数（口径可重跑）', () => {
  it('文案 / key / 标题的**字面值**逐条钉住（⛔ 不靠常量自指，改一个字就红）', () => {
    expect(PAGE_KEY).toBe('emp-settlementemp')
    expect(PAGE_TITLE).toBe('清算绩效')
    expect(MESSAGES.noStatus).toBe('请选择要修改的清算状态')
    expect(MESSAGES.syncNotOpen).toBe('「同步到账本」尚未开放，本次只改结算状态')
    expect(MESSAGES.alreadySameStatus).toBe('所选记录已是该状态，无需修改')
    expect(MESSAGES.reasonRequired).toBe('请填写撤销结账的原因（会写进订单日志）')
    expect(REASON_PROMPT_TITLE).toBe('仅修改结算状态')
    expect(QR_PHOTOS_TITLE).toBe('微信付款码')
  })

  it('`parsePerformanceIds`：逗号串 ⇒ 数字数组；空 / 非法段丢掉（不产生 NaN）', () => {
    expect(parsePerformanceIds('126508,126394')).toEqual([126508, 126394])
    expect(parsePerformanceIds('7')).toEqual([7])
    expect(parsePerformanceIds('')).toEqual([])
    expect(parsePerformanceIds(null)).toEqual([])
    expect(parsePerformanceIds('1,,x,2')).toEqual([1, 2])
  })

  it('`formatMoney`：两位小数；非数字回落 `0.00`（⛔ 不显示 NaN）', () => {
    expect(formatMoney('20')).toBe('20.00')
    expect(formatMoney('0.00')).toBe('0.00')
    expect(formatMoney('-15.5')).toBe('-15.50')
    expect(formatMoney('abc')).toBe('0.00')
  })

  it('笔数 / 金额两格的两种形态：`count>0 ⇒ X.XX元`，`count===0 ⇒ 0元`（**不是** `0.00元`）', () => {
    expect(countText(0)).toBe('0笔')
    expect(countText(2)).toBe('2笔')
    expect(moneyText(2, '20.00')).toBe('20.00元')
    expect(moneyText(1, '0.00')).toBe('0.00元')
    expect(moneyText(0, '0.00')).toBe('0元')
    // ⚠️ 判据取的是**笔数**：0 笔时哪怕后端给了非零总额也写 `0元`（证据件 L81/87 的形态）。
    expect(moneyText(0, '12.34')).toBe('0元')
  })

  it('结算对象格：`名字（金额）`，多人用全角逗号「，」隔开；空集 ⇒ 空串', () => {
    expect(
      ownersText([
        { user_id: 1, username: '测试昵称甲', amount: '4.00' },
        { user_id: 2, username: '测试昵称乙', amount: '1.60' },
      ]),
    ).toBe('测试昵称甲（4.00），测试昵称乙（1.60）')
    expect(ownersText([])).toBe('')
    // 归属人为空的那一项（后端把「无归属人」并成 `user_id: null` / `username: null`、排最后）
    // 不伪造成 0 行：名字位留空、金额照旧。
    expect(ownersText([{ user_id: null, username: null, amount: '0.00' }])).toBe('（0.00）')
  })

  it('⭐ 2026-10-04：名字位取 `name`（显示名），缺它或为 `null` 时回落 `username`', () => {
    // ① 后端已上（`name` 在）：显示名与登录账号**不同形** ⇒ 「拿 username 当 name」会红。
    expect(
      ownersText([
        { user_id: 1, username: 'kefu-zhang', name: '测试昵称张', amount: '4.00' },
      ]),
    ).toBe('测试昵称张（4.00）')
    // ② 部署窗口（前端先上、后端还没上 ⇒ 键缺席）：回落老键 `username`，⛔ 不显示空。
    expect(
      ownersText([{ user_id: 1, username: 'kefu-zhang', amount: '4.00' }]),
    ).toBe('kefu-zhang（4.00）')
    // ③ 后端把 `name` 显式给成 `null`（无归属人那一项的形状）⇒ 同样回落 `username`（那里也是 null）。
    expect(
      ownersText([
        { user_id: 1, username: 'kefu-zhang', name: null, amount: '4.00' },
        { user_id: null, username: null, name: null, amount: '0.00' },
      ]),
    ).toBe('kefu-zhang（4.00），（0.00）')
  })

  it('结账方向的提示：跳过 M 条就带上括号；`N===0` ⇒ 「所选记录已是该状态，无需修改」', () => {
    expect(settleResultText(2, 0)).toBe('已结账 2 条')
    expect(settleResultText(3, 2)).toBe('已结账 3 条（其中 2 条本来就是已结账，已跳过）')
    // 真「无需修改」= 绩效 id 列表与调整记录 id 列表**都**为空（第三个实参默认 0）。
    expect(settleResultText(0, 0)).toBe(MESSAGES.alreadySameStatus)
    expect(settleResultText(0, 2)).toBe(MESSAGES.alreadySameStatus)
  })

  it('⭐ 结账第三形态：绩效为空但**调整记录非空** ⇒ 如实报「已结清调整记录 N 条」（⛔ 不说无需修改）', () => {
    /**
     * 后端「已结账绩效 + 后来追加的未结账调整记录」那一轮（坏账出口，
     * `tests/test_settle_api.py::test_a_new_adjustment_on_a_settled_performance_is_settled_next_time`）：
     * `settled_performances == []`、`settled_adjustments == [id]` —— **钱动了**。
     * 旧实现只看第一个实参 ⇒ 命中 `N === 0` 弹出「无需修改」（那是**假话**）。
     */
    expect(settleResultText(0, 0, 1)).toBe('已结清调整记录 1 条')
    // 组合形态：同一轮里还有本来就是已结账、被跳过的绩效行。
    expect(settleResultText(0, 2, 1)).toBe(
      '已结清调整记录 1 条（其中 2 条本来就是已结账，已跳过）',
    )
    // 绩效与调整**都**结了的正常轮次：仍报绩效条数（调整不抢话）。
    expect(settleResultText(3, 0, 2)).toBe('已结账 3 条')
  })

  it('撤销方向的提示：与结账同形换词（「已撤销结账」/「本来就是未结账」）', () => {
    expect(revokeResultText(2, 0)).toBe('已撤销结账 2 条')
    expect(revokeResultText(1, 1)).toBe('已撤销结账 1 条（其中 1 条本来就是未结账，已跳过）')
    expect(revokeResultText(0, 0)).toBe(MESSAGES.alreadySameStatus)
    expect(revokeResultText(0, 1)).toBe(MESSAGES.alreadySameStatus)
  })

  it('⭐ 撤销第三形态：与结账**形状对称**（⚠️ 后端今天到不了这一轮，见函数 docstring）', () => {
    /**
     * ⚠️ **可达性如实登记**：`performance_writes.revoke_settlements` 的
     * `revoked_adjustments` 只从命中的已结账绩效行派生（`pairs` 由 `revoked_rows` 构造）
     * ⇒ 「`revoked_performances` 为空 + `revoked_adjustments` 非空」在后端**到不了**。
     * 本用例钉的是**两个端点返回体同形**这件事在文案函数上也对称（改坏第三形态必红）。
     */
    expect(revokeResultText(0, 0, 1)).toBe('已撤销结账的调整记录 1 条')
    expect(revokeResultText(0, 2, 1)).toBe(
      '已撤销结账的调整记录 1 条（其中 2 条本来就是未结账，已跳过）',
    )
    expect(revokeResultText(2, 0, 1)).toBe('已撤销结账 2 条')
  })

  it('`reloadParentList`：父页有 `tools.reloadOrderData` ⇒ 调它（`this` 绑到 `tools`），不 closeAll', () => {
    const seen: unknown[] = []
    const tools = {
      reloadOrderData(this: unknown) {
        seen.push(this)
      },
    }
    const layer = { closeAll: vi.fn() }
    reloadParentList({ tools }, layer as never)
    expect(seen).toEqual([tools])
    expect(layer.closeAll).not.toHaveBeenCalled()
  })

  it('`reloadParentList`：父页没有那个工具 ⇒ 退回 `layer.closeAll()`，⛔ 不抛', () => {
    const layer = { closeAll: vi.fn() }
    expect(() => reloadParentList({}, layer as never)).not.toThrow()
    expect(layer.closeAll).toHaveBeenCalledTimes(1)
    // 有 tools 但没有那个函数 / 它不是函数 ⇒ 同样走兜底。
    const layer2 = { closeAll: vi.fn() }
    reloadParentList({ tools: {} }, layer2 as never)
    expect(layer2.closeAll).toHaveBeenCalledTimes(1)
    const layer3 = { closeAll: vi.fn() }
    reloadParentList({ tools: { reloadOrderData: 'x' as never } }, layer3 as never)
    expect(layer3.closeAll).toHaveBeenCalledTimes(1)
  })
})

/* ────── `window.parent` 的桩：jsdom 里 `window.parent === window`，不换掉就测不出承载差异 ────── */

/**
 * jsdom 里 `window.parent === window` ⇒ 直接写 `window.parent.tools = …` 落的其实是**本页自己的**
 * `window`，于是「读 `window`」与「读 `window.parent`」两条实现**读数完全一样**、一条都区分不出来
 * ——真机缺陷（`reloadParentList(window, layer)`：结算成功却弹层不关、父页列表不刷）正是这样漏过单测的。
 *
 * 这里用 `Object.defineProperty` 把实例上的 `parent` 访问器换成一个**独立的假窗口对象**，两条实现
 * 从此分道扬镳：读本页 `window` ⇒ `undefined`（只能走 `closeAll` 兜底），读 `window.parent` ⇒ 命中桩。
 *
 * ⚠️ 还原必须用**存下来的描述符原样写回**（`restoreParentWindow`），⛔ 不能用 `delete window.parent`：
 * 本机实测 delete 之后 `window.parent` 变成 `undefined`（不再是 window 自己）。
 */
let savedParentDescriptor: PropertyDescriptor | undefined

/** 把 `window.parent` 换成假窗口（默认一个空对象 = 父页没有任何工具）。返回那个假窗口。 */
function stubParentWindow(parent: Record<string, unknown> = {}): Record<string, unknown> {
  if (savedParentDescriptor === undefined) {
    savedParentDescriptor = Object.getOwnPropertyDescriptor(window, 'parent')
  }
  Object.defineProperty(window, 'parent', { value: parent, configurable: true, writable: true })
  return parent
}

function restoreParentWindow(): void {
  const saved = savedParentDescriptor
  if (saved === undefined) return
  Object.defineProperty(window, 'parent', saved)
  savedParentDescriptor = undefined
}

/* ─────────────────────── 驱动真交互：真挂载 + 真点击 ─────────────────────── */

describe('清算绩效子页：挂载后的真实流程', () => {
  let root: HTMLElement

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.formHandlers.clear()
    LAYUI.renders.length = 0
    document.body.innerHTML = ''
    root = document.createElement('div')
    root.id = 'app'
    document.body.appendChild(root)
    vi.mocked(fetchSettlementPreview).mockResolvedValue(PREVIEW as never)
    vi.mocked(settlePerformances).mockResolvedValue({
      settled_performances: [1, 2],
      settled_adjustments: [],
      skipped_performances: [],
    } as never)
    vi.mocked(revokeSettlements).mockResolvedValue({
      revoked_performances: [1],
      revoked_adjustments: [],
      skipped_performances: [],
    } as never)
    setSearch('1,2')
  })

  afterEach(() => {
    window.history.replaceState({}, '', '/')
    restoreParentWindow()
    vi.unstubAllGlobals()
  })

  async function mountPage(ids = '1,2'): Promise<void> {
    setSearch(ids)
    await page.mount(root, {} as never)
    await flush()
  }

  /** 触发「确定结算或修改」（layui 的 `submit(savebtn)` 回调由测试手动派发，同仓既有写法）。 */
  const clickSave = (): unknown => LAYUI.formHandlers.get('submit(savebtn)')!({})

  /** 真点一下某个清算状态 radio，再按 layui 的行为派发 `radio(sta)`。 */
  function pickSta(value: 'N' | 'Y'): void {
    const input = root.querySelector<HTMLInputElement>(`input[name="sta"][value="${value}"]`)!
    input.click()
    const handler = LAYUI.formHandlers.get('radio(sta)')
    expect(handler, '页面必须注册 `radio(sta)`').toBeTypeOf('function')
    handler!({ value })
  }

  /** 展开区（两个 `.expdiv`）当前的 `display`。 */
  const expandDisplays = (): string[] =>
    Array.from(root.querySelectorAll<HTMLElement>('.expdiv')).map((d) => d.style.display)

  /** 打开原因层并把它的回调交出来（`submit` 之后 `layer.prompt` 的第二个实参）。 */
  function openReasonPrompt(): (value: string, index: number) => void {
    const handler = LAYUI.formHandlers.get('submit(savebtn)')
    expect(handler, '页面必须注册 `submit(savebtn)`').toBeTypeOf('function')
    handler!({})
    const call = lastCall(LAYUI.prompt)
    expect(call, '选「未结算」提交后必须先弹原因层').toBeTruthy()
    return call![1] as (value: string, index: number) => void
  }

  it('挂载：先挂样式再塞标记（`initIslandElements` 在塞标记之后），四格按预览端点填对', async () => {
    await mountPage()
    expect(vi.mocked(fetchSettlementPreview)).toHaveBeenCalledWith([1, 2])
    expect(vi.mocked(initIslandElements)).toHaveBeenCalled()
    // 无参 render（传类型只会渲染一种控件，另一种会点不了）。
    expect(LAYUI.renders).toContain('')
    const text = (label: string): string | undefined =>
      Array.from(root.querySelectorAll<HTMLElement>('.layui-form-item'))
        .find((item) => item.querySelector('.layui-form-label')?.textContent?.trim() === label)
        ?.querySelector<HTMLElement>('.layui-input-block > span')?.textContent ?? undefined
    expect(text(CELL_LABELS.count)).toBe('2笔')
    expect(text(CELL_LABELS.tempTotal)).toBe('20.00元')
    expect(text(CELL_LABELS.bonusTotal)).toBe('0.00元')
    expect(text(CELL_LABELS.owners)).toBe('测试昵称甲（0.00）')
    expect(root.querySelector('.qrCodeBox img')?.getAttribute('src')).toBe(
      '/media/qr/3.png?sig=abc',
    )
    expect(document.title).toBe(PAGE_TITLE)
    expect(document.body.id).toBe('iosiframe')
  })

  it('⭐ 2026-10-04：结算对象格显示**昵称**、不显示登录账号（后端 `owners[].name`，spec §2.4-C）', async () => {
    vi.mocked(fetchSettlementPreview).mockResolvedValue({
      ...PREVIEW,
      owners: [{ user_id: 3, username: 'kefu-zhang', name: '测试昵称张', amount: '0.00' }],
    } as never)
    await mountPage()
    const block = Array.from(root.querySelectorAll<HTMLElement>('.layui-form-item')).find(
      (item) => item.querySelector('.layui-form-label')?.textContent?.trim() === CELL_LABELS.owners,
    )
    const ownersSpan = block?.querySelector<HTMLElement>('.layui-input-block > span')
    expect(ownersSpan?.textContent).toBe('测试昵称张（0.00）')
    // 判据的另一半（业务方原话「显示的是账号而不是账号名称」）：登录账号**不出现**。
    expect(ownersSpan?.textContent).not.toContain('kefu-zhang')
  })

  it('`count === 0` 时两格显示 `0元`（**不是** `0.00元`），结算对象为空', async () => {
    vi.mocked(fetchSettlementPreview).mockResolvedValue(EMPTY_PREVIEW as never)
    await mountPage('999999999')
    const spans = Array.from(
      root.querySelectorAll<HTMLElement>('.layui-form-item > .layui-input-block > span'),
    )
    expect(spans.map((s) => s.textContent)).toEqual(['0笔', '0元', '0元', ''])
  })

  it('点 radio：选「已结算」⇒ 两个 `.expdiv` 展开；选「未结算」⇒ 收回', async () => {
    await mountPage()
    expect(expandDisplays()).toEqual(['none', 'none'])
    pickSta('Y')
    expect(expandDisplays()).toEqual(['', ''])
    pickSta('N')
    expect(expandDisplays()).toEqual(['none', 'none'])
  })

  it('**没选**清算状态就点「确定结算或修改」⇒ 提示 + **零请求**（连原因层都不弹）', async () => {
    await mountPage()
    expect(clickSave()).toBe(false)
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith(MESSAGES.noStatus, { time: 1500 })
    expect(vi.mocked(fetchSettlementPreview)).toHaveBeenCalledTimes(1) // 只有挂载那一次
    expect(vi.mocked(settlePerformances)).not.toHaveBeenCalled()
    expect(vi.mocked(revokeSettlements)).not.toHaveBeenCalled()
    expect(LAYUI.prompt).not.toHaveBeenCalled()
  })

  it('选「未结算」提交 ⇒ **先弹原因层**（`formType:2`），此时一个请求都不发', async () => {
    await mountPage()
    pickSta('N')
    const cb = openReasonPrompt()
    expect(typeof cb).toBe('function')
    const opts = lastCall(LAYUI.prompt)![0] as Record<string, unknown>
    expect(opts.formType).toBe(2)
    expect(String(opts.placeholder)).toContain('2 条绩效') // 勾选条数取自隐藏域
    await flush()
    expect(vi.mocked(revokeSettlements)).not.toHaveBeenCalled()
    expect(vi.mocked(settlePerformances)).not.toHaveBeenCalled()
  })

  it('原因空 / 纯空白 ⇒ 提示、**不关层**、不发任何请求（层还在，可补填）', async () => {
    await mountPage()
    pickSta('N')
    const cb = openReasonPrompt()
    cb('   ', 7)
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith(MESSAGES.reasonRequired, { time: 1500 })
    expect(LAYUI.close).not.toHaveBeenCalled()
    expect(vi.mocked(revokeSettlements)).not.toHaveBeenCalled()
  })

  it('原因填了 ⇒ 关层 + 打 `revoke`，**原因随请求走**，提示按后端返回的 id 列表报数', async () => {
    vi.mocked(revokeSettlements).mockResolvedValue({
      revoked_performances: [1],
      revoked_adjustments: [],
      skipped_performances: [2],
    } as never)
    await mountPage()
    pickSta('N')
    const cb = openReasonPrompt()
    cb('  财务核对后撤销  ', 7)
    await flush()
    expect(LAYUI.close).toHaveBeenCalledWith(7)
    expect(vi.mocked(revokeSettlements)).toHaveBeenCalledWith([1, 2], '财务核对后撤销')
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '已撤销结账 1 条（其中 1 条本来就是未结账，已跳过）',
      { time: 1000 },
      expect.any(Function),
    )
  })

  it('选「已结算」提交 ⇒ 直接打 `settle`（**不弹原因层**）；跳过条数如实报数', async () => {
    vi.mocked(settlePerformances).mockResolvedValue({
      settled_performances: [1, 2],
      settled_adjustments: [],
      skipped_performances: [3],
    } as never)
    await mountPage()
    pickSta('Y')
    clickSave()
    await flush()
    expect(LAYUI.prompt).not.toHaveBeenCalled()
    expect(vi.mocked(settlePerformances)).toHaveBeenCalledWith([1, 2], '')
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '已结账 2 条（其中 1 条本来就是已结账，已跳过）',
      { time: 1000 },
      expect.any(Function),
    )
  })

  it('全批都已是目标状态（`N === 0`）⇒ 标题「所选记录已是该状态，无需修改」', async () => {
    vi.mocked(settlePerformances).mockResolvedValue({
      settled_performances: [],
      settled_adjustments: [],
      skipped_performances: [1, 2],
    } as never)
    await mountPage()
    pickSta('Y')
    clickSave()
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith(
      MESSAGES.alreadySameStatus,
      { time: 1000 },
      expect.any(Function),
    )
  })

  it('⭐ 坏账出口那一轮（绩效为空 + 调整记录有 + 跳过有）⇒ 弹「已结清调整记录 1 条（…已跳过）」，⛔ 不弹「无需修改」', async () => {
    // 后端口径见 `tests/test_settle_api.py` 的
    // `test_a_new_adjustment_on_a_settled_performance_is_settled_next_time`
    // （`settled_performances == []` / `settled_adjustments == [adjustment.id]`）。
    // 这里再叠一条「本来就是已结账、被跳过」的绩效行，覆盖**组合形态**。
    vi.mocked(settlePerformances).mockResolvedValue({
      settled_performances: [],
      settled_adjustments: [91],
      skipped_performances: [1, 2],
    } as never)
    await mountPage()
    pickSta('Y')
    clickSave()
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '已结清调整记录 1 条（其中 2 条本来就是已结账，已跳过）',
      { time: 1000 },
      expect.any(Function),
    )
    expect(LAYUI.msg).not.toHaveBeenCalledWith(
      MESSAGES.alreadySameStatus,
      { time: 1000 },
      expect.any(Function),
    )
  })

  it('撤销方向的第三形态也接上了（`revoked_performances` 空 + `revoked_adjustments` 非空）', async () => {
    vi.mocked(revokeSettlements).mockResolvedValue({
      revoked_performances: [],
      revoked_adjustments: [91],
      skipped_performances: [],
    } as never)
    await mountPage()
    pickSta('N')
    const cb = openReasonPrompt()
    cb('财务核对后撤销', 7)
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '已撤销结账的调整记录 1 条',
      { time: 1000 },
      expect.any(Function),
    )
  })

  it('Q4=A：勾了「同步到账本 = 需要」⇒ 先提示「尚未开放」，但**照旧**打 settle（不阻断）', async () => {
    await mountPage()
    pickSta('Y')
    root.querySelector<HTMLInputElement>('input[name="syncexp"][value="Y"]')!.checked = true
    clickSave()
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith(MESSAGES.syncNotOpen, { time: 1500 })
    expect(vi.mocked(settlePerformances)).toHaveBeenCalledWith([1, 2], '')
  })

  it('成功后由提示的回调刷新父页：**父窗口**的 `parent.tools.reloadOrderData()` 真被调到', async () => {
    const reload = vi.fn()
    // ⚠️ 桩挂在**假父窗口**上（`stubParentWindow`）：`window.parent === window` 时写
    //    `window.parent.tools` 等于写在本页自己的 window 上 ⇒「读 `window`」那条错误实现也会绿。
    //    紧接着那行是**自证两条实现真的分道扬镳**：本页自己的 `window` 上没有 `tools`。
    stubParentWindow({ tools: { reloadOrderData: reload } })
    expect((window as unknown as { tools?: unknown }).tools).toBeUndefined()
    await mountPage()
    pickSta('Y')
    clickSave()
    await flush()
    const last = lastCall(LAYUI.msg)!
    expect(last[0]).toBe('已结账 2 条')
    expect(typeof last[2]).toBe('function')
    ;(last[2] as () => void)()
    expect(reload).toHaveBeenCalledTimes(1)
    // ⛔ 走的是父页那条路，**不是**兜底（读本页 `window` 的实现两条断言都会红）。
    expect(LAYUI.closeAll).not.toHaveBeenCalled()
  })

  it('父页**没有** `tools` ⇒ 成功回调退回 `layer.closeAll()`，⛔ 不抛', async () => {
    stubParentWindow() // 父窗口在，但没有 `tools` 槽位
    await mountPage()
    pickSta('Y')
    clickSave()
    await flush()
    const last = lastCall(LAYUI.msg)!
    expect(() => (last[2] as () => void)()).not.toThrow()
    expect(LAYUI.closeAll).toHaveBeenCalledTimes(1)
  })

  it('父窗口上**读 `tools` 就抛**（跨域形态）⇒ 同样走兜底，⛔ 不抛给调用方', async () => {
    stubParentWindow({
      get tools(): unknown {
        throw new Error('SecurityError: blocked a frame from accessing a cross-origin frame')
      },
    })
    await mountPage()
    pickSta('Y')
    clickSave()
    await flush()
    const last = lastCall(LAYUI.msg)!
    expect(() => (last[2] as () => void)()).not.toThrow()
    expect(LAYUI.closeAll).toHaveBeenCalledTimes(1)
  })

  it('`window.parent` 自己取不到（访问器抛）⇒ `parentToolsWindow()` 得 `undefined`，回调照旧不抛', async () => {
    // 本用例自己换描述符（`stubParentWindow` 的存档另算），所以自己 `try/finally` 还原。
    const saved = Object.getOwnPropertyDescriptor(window, 'parent')
    Object.defineProperty(window, 'parent', {
      get(): unknown {
        throw new Error('SecurityError: blocked a frame from accessing a cross-origin frame')
      },
      configurable: true,
    })
    try {
      expect(parentToolsWindow()).toBeUndefined()
      await mountPage()
      pickSta('Y')
      clickSave()
      await flush()
      const last = lastCall(LAYUI.msg)!
      expect(typeof last[2]).toBe('function')
      expect(() => (last[2] as () => void)()).not.toThrow()
      expect(LAYUI.closeAll).toHaveBeenCalledTimes(1)
    } finally {
      if (saved !== undefined) Object.defineProperty(window, 'parent', saved)
    }
  })

  it('取数失败 ⇒ 如实提示后端文案，⛔ 不把「取不到」画成金额（保留证据件零态）', async () => {
    vi.mocked(fetchSettlementPreview).mockRejectedValue(
      new Error('没有权限执行该操作') as never,
    )
    await mountPage()
    expect(LAYUI.msg).toHaveBeenCalledWith('没有权限执行该操作', { time: 1500 })
    const spans = Array.from(
      root.querySelectorAll<HTMLElement>('.layui-form-item > .layui-input-block > span'),
    )
    expect(spans.map((s) => s.textContent)).toEqual(['0笔', '0元', '0元', ''])
  })

  it('请求失败 ⇒ 只显示后端文案、**不**刷新父页、可重试（`inflight` 已复位）', async () => {
    const reload = vi.fn()
    stubParentWindow({ tools: { reloadOrderData: reload } })
    vi.mocked(settlePerformances).mockRejectedValue(new Error('状态不允许') as never)
    await mountPage()
    pickSta('Y')
    clickSave()
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith('状态不允许', { time: 1500 })
    expect(reload).not.toHaveBeenCalled()
    // 复位后可再试：第二次调用真的又发出去了。
    vi.mocked(settlePerformances).mockResolvedValue({
      settled_performances: [1],
      settled_adjustments: [],
      skipped_performances: [],
    } as never)
    clickSave()
    await flush()
    expect(vi.mocked(settlePerformances)).toHaveBeenCalledTimes(2)
  })

  it('点收款码 ⇒ `layer.photos`（标题逐字 `微信付款码`，`start` 是 data-index 字符串）', async () => {
    await mountPage()
    const img = root.querySelector<HTMLImageElement>('.qrCodeImg')!
    img.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.photos).toHaveBeenCalledWith({
      photos: {
        title: QR_PHOTOS_TITLE,
        start: '0',
        data: [{ alt: '测试昵称甲', pid: 0, src: '/media/qr/3.png?sig=abc' }],
      },
    })
  })

  it('⭐ QR 大图 `data` 里的 `alt` / `src` 也过 `escapeHtml`（vendored layui 把它们拼进 HTML 属性）', async () => {
    /**
     * 判据来源（**能失败的用例**）：vendored layui 的 `layer.photos` 把这两格**字符串拼进
     * `<img src="…" alt="…">`**（`src/frontend/public/layui/layui.js` 的 layer 模块，读码核过）
     * ⇒ 一个 `x" onerror="alert(1)` 形态的用户名会从**属性**里长出一个新属性。
     *
     * ⚠️ 只在 `qrCodesMarkup` 那条路转义是**不够**的：那是我们自己拼的 `innerHTML` 字符串，
     * 而这里是**把值交给第三方库去拼**——两条出口各要各的转义（文件头「出口安全」表第 2 条）。
     * 本用例对 `alt` 断「**没有裸引号**」+「`"` 变成了 `&quot;`」，改回裸值时必红。
     */
    vi.mocked(fetchSettlementPreview).mockResolvedValue({
      ...PREVIEW,
      qr_codes: [
        {
          user_id: 3,
          username: 'x" onerror="alert(1)',
          name: '测试姓名甲',
          url: '/media/qr/3.png?a=1&b="2"',
        },
      ],
    } as never)
    await mountPage()
    const img = root.querySelector<HTMLImageElement>('.qrCodeImg')!
    img.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const data = (
      (lastCall(LAYUI.photos)![0] as { photos: { data: { alt: string; src: string }[] } })
        .photos.data
    )
    expect(data).toHaveLength(1)
    // `alt`：裸引号必须消失、`"` 必须是 `&quot;`（否则 `alt="…"` 会被提前闭合）。
    expect(data[0]!.alt).not.toContain('"')
    expect(data[0]!.alt).toContain('&quot;')
    expect(data[0]!.alt).toBe('x&quot; onerror=&quot;alert(1)')
    // `src`：`&` 转成 `&amp;`（HTML 解析后会还原成 `&`，但不会逃出属性）、引号同样转义。
    expect(data[0]!.src).not.toContain('"')
    expect(data[0]!.src).toBe('/media/qr/3.png?a=1&amp;b=&quot;2&quot;')
  })

})
