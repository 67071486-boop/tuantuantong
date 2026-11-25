// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SystemSwitchRow } from '@/api/systemSwitches'

/**
 * 【系统设置】island 页（`pages/system-systemsetting.ts`）的契约测试。
 *
 * ⭐ 系统开关线（D-0223，2026-10-10）：本页从 `artifactPage` 改为「自取证据件 + 自有委托」
 * （只接 SK32 一条开关）。本文件证六件事：
 *
 * 1. 常量与证据件对齐（SK32 两个 radio / 5 颗 `save2btn` / `lay-filter="sform"` 都不是凭记忆写的）；
 * 2. **载入后 SK32 勾选态来自 GET 返回**（证据件里 `checked` 是对标租户的数据，⛔ 不是我们的默认）；
 * 3. 点 radio + 保存 ⇒ `updateSystemSwitch` 收到正确的 key 与值（真 `data.field` 与 DOM 两条取值路）；
 * 4. 点**其他**设置（SK10 / 清除绑定按钮）⇒ 仍弹「尚未开放」（自建总闸没有整体关掉）；
 * 5. 页签标题不算「尚未开放」控件（I-1 回归，自建总闸沿用 `islandTabTarget` 判据）；
 * 6. PATCH 失败 ⇒ 原样弹后端 msg、勾选态回滚到保存前的值。
 *
 * 手法照 `system-autodiscernset.spec.ts`：`vi.mock` 掉 `@/layui/island` 的
 * `bootstrapIsland`（jsdom 里加载不了真 layui）与 `@/api/systemSwitches`，
 * 只断言**入参**；证据件用**真实文件**（96 KB），不另造一份标记。
 *
 * ⚠️ `form.val` 的替身刻意实现成「radio 按 `value` 勾选」——这正是「勾选态来自 GET」
 * 那条的判据（替身若只会设 input.value，radio 的 checked 永远不动，断言恒红）。
 */

vi.mock('@/api/systemSwitches', () => ({
  fetchSystemSwitches: vi.fn(),
  updateSystemSwitch: vi.fn(),
}))

/** layui 的可控替身（`layer.msg` 是「尚未开放」/保存提示的出口）。 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const formHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const formVals: Array<Record<string, unknown>> = []
  return {
    msg,
    formHandlers,
    formVals,
    form: {
      render: vi.fn(),
      // 替身按 `<form lay-filter>` 的作用域找输入；radio 按 value 勾选（见文件头注释）。
      val: vi.fn((filter: string, data: Record<string, unknown>) => {
        formVals.push(data)
        const form = document.querySelector<HTMLFormElement>(`form[lay-filter="${filter}"]`)
        if (!form) return
        for (const [name, value] of Object.entries(data)) {
          form.querySelectorAll<HTMLInputElement>(`[name="${name}"]`).forEach((input) => {
            if (input.type === 'radio') input.checked = input.value === String(value)
            else input.value = String(value)
          })
        }
      }),
      on: (e: string, cb: (...args: unknown[]) => unknown) => formHandlers.set(e, cb),
    },
    table: { render: vi.fn(), reload: vi.fn() },
    layer: { msg, open: vi.fn(), close: vi.fn() },
    element: { init: vi.fn() },
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
    // 页签类名切换不在本文件的判据面里（`emp-emplist.spec.ts` 驱动真件）。
    initIslandElements: vi.fn(),
  }
})

import { fetchSystemSwitches, updateSystemSwitch } from '@/api/systemSwitches'
import { BizError } from '@/api/errors'
import {
  checkedSk32Value,
  FORM_FILTER,
  mountSystemSettingPage,
  SAVE_FILTER,
  SK32_RADIO_NAME,
  SK32_SWITCH_KEY,
  SYSTEMSETTING_COLS,
  TITLE,
} from '../system-systemsetting'

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 证据件原文（`public/replica/system-systemsetting.html`；4 层上去 = `src/frontend`）。 */
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/system-systemsetting.html'),
  'utf8',
)

/** 后端 GET 的行形状（今天恰好一条；值按用例需要覆盖）。 */
function rows(value: 'Y' | 'N'): { items: SystemSwitchRow[] } {
  return {
    items: [
      {
        key: SK32_SWITCH_KEY,
        name: '录单时禁止录入相同的来源单号',
        value,
        default: 'N',
        replica_sk: 'SK32',
      },
    ],
  }
}

function textResponse(text: string): Response {
  return { ok: true, status: 200, text: async () => text } as unknown as Response
}

/** 挂载整页：证据件走全局 fetch（真文件），开关列表走 `@/api/systemSwitches` 的替身。 */
async function mount(
  value: 'Y' | 'N' = 'N',
): Promise<{ root: HTMLElement; cleanup: () => void }> {
  vi.mocked(fetchSystemSwitches).mockResolvedValue(rows(value))
  const root = document.createElement('div')
  document.body.appendChild(root)
  const cleanup = await mountSystemSettingPage(root, { onNavigate: vi.fn() })
  // 等首次载入的勾选态落进 DOM（reload() 是异步的）。
  await vi.waitFor(() => expect(checkedSk32Value(root)).toBe(value))
  return { root, cleanup }
}

/** 页内某个名字的 radio（原生 input；jsdom 里 `.click()` 会真正切换 checked）。 */
function radio(root: HTMLElement, name: string, value: string): HTMLInputElement {
  const input = root.querySelector<HTMLInputElement>(
    `input[name="${name}"][value="${value}"]`,
  )
  expect(input, `证据件里应有 input[name="${name}"][value="${value}"]`).toBeTruthy()
  return input!
}

beforeEach(() => {
  vi.clearAllMocks()
  LAYUI.formHandlers.clear()
  LAYUI.formVals.length = 0
  vi.mocked(fetchSystemSwitches).mockReset()
  vi.mocked(updateSystemSwitch).mockReset()
  vi.mocked(updateSystemSwitch).mockResolvedValue({
    key: SK32_SWITCH_KEY,
    name: '录单时禁止录入相同的来源单号',
    value: 'Y',
  })
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('system-systemsetting.html')) return textResponse(EVIDENCE)
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

/* ─────────────── 常量与证据件对齐 ─────────────── */

describe('常量与证据件：外观与接真位点的唯一出处是 system-systemsetting.html', () => {
  it('页 title / 表单 filter / 保存 filter / SK32 的 name 逐字取值', () => {
    expect(TITLE).toBe('系统设置')
    expect(FORM_FILTER).toBe('sform')
    expect(SAVE_FILTER).toBe('save2btn')
    expect(SK32_RADIO_NAME).toBe('SK32')
  })

  it('证据件里真有这些标记（常量不是凭记忆写的）', () => {
    expect(EVIDENCE).toContain('lay-filter="sform"')
    // SK32 的两个 radio（L247-248）：值 N=「不要求」/ Y=「是的不允许」。
    expect(EVIDENCE).toContain('name="SK32" value="N" title="不要求"')
    expect(EVIDENCE).toContain('name="SK32" value="Y" title="是的不允许"')
    // 保存按钮：5 颗共用同一个 lay-filter（对标的 form.on 也只有一个）。
    expect(EVIDENCE.match(/lay-filter="save2btn"/g)).toHaveLength(5)
    // 列定义引用的工具栏模板与表格 elem。
    expect(EVIDENCE).toContain('id="tableList"')
    expect(EVIDENCE).toContain('id="toolbarDemo"')
  })
})

/* ─────────────── 主表列（对标 systemsetting.js L165-193，防漂移）─────────────── */

describe('系统设置页的主表列（对标 systemsetting.js L165-193）', () => {
  it('5 列，标题与顺序逐字对齐对标', () => {
    expect(SYSTEMSETTING_COLS.map((c) => c.title)).toEqual([
      '标题',
      '热度(越大排越前)',
      '是否需要显示出来',
      '发布时间',
      '操作',
    ])
  })

  it('列宽取桌面分支（`isPc` 为真那一支）的百分数字面量', () => {
    expect(SYSTEMSETTING_COLS.map((c) => String(c.width))).toEqual([
      '30%',
      '20%',
      '12%',
      '12%',
      '12%',
    ])
  })

  it('对齐与 `unresize` 照对标（只有「操作」列 unresize）；没有一列是 fixed', () => {
    expect(SYSTEMSETTING_COLS.map((c) => c.align)).toEqual([
      'left',
      'center',
      'center',
      'center',
      undefined,
    ])
    expect(SYSTEMSETTING_COLS.map((c) => c.unresize === true)).toEqual([
      false,
      false,
      false,
      false,
      true,
    ])
    expect(SYSTEMSETTING_COLS.every((c) => c.fixed === undefined)).toBe(true)
  })
})

/* ─────────────── 挂载声明 + 载入勾选态 ─────────────── */

describe('挂载：表格声明按对标原值；SK32 勾选态来自 GET', () => {
  it('table.render：toolbar / defaultToolbar / page:false / limit 10 / limits [10]（对标 L170-194）', async () => {
    const { cleanup } = await mount()
    const cfg = LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown>
    expect(cfg.elem).toBe('#tableList')
    expect(cfg.id).toBe('tableList')
    expect(cfg.toolbar).toBe('#toolbarDemo')
    expect(cfg.defaultToolbar).toEqual(['filter'])
    expect(cfg.page).toBe(false)
    expect(cfg.limit).toBe(10)
    expect(cfg.limits).toEqual([10])
    expect(cfg.data).toEqual([])
    expect(document.title).toBe('系统设置')
    cleanup()
  })

  it('⭐ 载入后 SK32 勾选态来自 GET 返回（证据件的 checked=Y 被覆盖成 N）', async () => {
    const { root, cleanup } = await mount('N')
    // 前置哨兵：证据件里 Y 本来是勾中的（否则「覆盖」这条判据恒真）。
    expect(EVIDENCE).toContain('name="SK32" value="Y" title="是的不允许" checked=')
    expect(checkedSk32Value(root)).toBe('N')
    expect(radio(root, 'SK32', 'N').checked).toBe(true)
    expect(radio(root, 'SK32', 'Y').checked).toBe(false)
    // `form.val` 用的是证据件那把 filter（写错 'form' 预填会静默失效）。
    expect(LAYUI.formVals[0]).toEqual({ SK32: 'N' })
    cleanup()
  })

  it('GET 返回 Y ⇒ 勾 Y（两个方向都有判别力）', async () => {
    const { root, cleanup } = await mount('Y')
    expect(checkedSk32Value(root)).toBe('Y')
    cleanup()
  })

  it('取数失败 → msg(e.message)，不抛、不留证据件里的假勾选态当真值', async () => {
    vi.mocked(fetchSystemSwitches).mockRejectedValue(new BizError(403, '没有权限'))
    const root = document.createElement('div')
    document.body.appendChild(root)
    const cleanup = await mountSystemSettingPage(root, { onNavigate: vi.fn() })
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('没有权限', { time: 1500 }))
    cleanup()
  })
})

/* ─────────────── 保存：PATCH 载荷 / 成功 / 失败回滚 ─────────────── */

describe('保存：只送 SK32 一条（对标是全量表单 JSON——「已知会不同」）', () => {
  it('点 Y 的 radio + 保存（DOM 取值路）⇒ PATCH (key, "Y")，成功提示并重取勾选态', async () => {
    const { root, cleanup } = await mount('N')
    radio(root, 'SK32', 'Y').click()
    expect(checkedSk32Value(root)).toBe('Y')
    LAYUI.formHandlers.get(`submit(${SAVE_FILTER})`)!()
    await vi.waitFor(() => expect(updateSystemSwitch).toHaveBeenCalledTimes(1))
    expect(updateSystemSwitch).toHaveBeenCalledWith(SK32_SWITCH_KEY, 'Y')
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('保存成功', { time: 1500 }))
    // 成功后重取一次（对标 location.reload 的等效）：GET 被调了两次（载入 + 保存后）。
    await vi.waitFor(() => expect(fetchSystemSwitches).toHaveBeenCalledTimes(2))
    cleanup()
  })

  it('真 layui 那条路：`data.field.SK32` 优先（整表单 60+ 键里只读这一个）', async () => {
    const { cleanup } = await mount('N')
    LAYUI.formHandlers.get(`submit(${SAVE_FILTER})`)!({
      field: { SK10: 'Y', SK32: 'Y', 别的键: 'x' },
    })
    await vi.waitFor(() => expect(updateSystemSwitch).toHaveBeenCalledTimes(1))
    expect(updateSystemSwitch).toHaveBeenCalledWith(SK32_SWITCH_KEY, 'Y')
    cleanup()
  })

  it('失败 ⇒ 原样弹后端 msg、勾选回滚到保存前的值、不发「保存成功」', async () => {
    vi.mocked(updateSystemSwitch).mockRejectedValue(new BizError(1000, '开关值必须是 Y 或 N'))
    const { root, cleanup } = await mount('N')
    radio(root, 'SK32', 'Y').click()
    expect(checkedSk32Value(root)).toBe('Y')
    LAYUI.formHandlers.get(`submit(${SAVE_FILTER})`)!()
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenCalledWith('开关值必须是 Y 或 N', { time: 1500 }),
    )
    expect(LAYUI.msg).not.toHaveBeenCalledWith('保存成功', { time: 1500 })
    // 勾选态回滚：Y 弹回 N（否则「没保存上」被伪装成「已保存」）。
    expect(checkedSk32Value(root)).toBe('N')
    // 失败不重取（勾选回滚在本地完成，不需要再问后端）。
    expect(fetchSystemSwitches).toHaveBeenCalledTimes(1)
    cleanup()
  })
})

/* ─────────────── 「尚未开放」总闸（自建；两条放行路之外全拦）─────────────── */

describe('真 DOM：总闸只放过 SK32 与保存按钮，其余照弹「尚未开放」', () => {
  it('点 SK10 的 radio（别的设置项）⇒ 仍弹「尚未开放」', async () => {
    const { root, cleanup } = await mount('N')
    radio(root, 'SK10', 'Y').click()
    expect(LAYUI.msg).toHaveBeenCalledTimes(1)
    expect(LAYUI.msg.mock.calls[0]![0]).toContain('尚未开放')
    // 放行对照：点 SK32 的 radio ⇒ 零 toast（它接了真）。
    LAYUI.msg.mockClear()
    radio(root, 'SK32', 'Y').click()
    expect(LAYUI.msg).not.toHaveBeenCalled()
    cleanup()
  })

  it('点保存按钮本体 ⇒ 零 toast（submit 委托自己接），点其它按钮 ⇒ 照弹', async () => {
    const { root, cleanup } = await mount('N')
    // 5 颗 save2btn 全部放行。
    for (const btn of root.querySelectorAll<HTMLElement>('[lay-filter="save2btn"]')) {
      btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    }
    expect(LAYUI.msg).not.toHaveBeenCalled()
    // 阳性对照：`clearBindbtn`（证据件 L134 的「清除绑定」按钮）⇒ 照弹。
    const clear = root.querySelector<HTMLElement>('button[lay-filter="clearBindbtn"]')
    expect(clear, '证据件里应当有 clearBindbtn').toBeTruthy()
    clear!.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    expect(LAYUI.msg).toHaveBeenCalledTimes(1)
    expect(LAYUI.msg).toHaveBeenCalledWith('「清除绑定」尚未开放', { time: 1500 })
    cleanup()
  })

  it('I-1 回归：点页签标题 ⇒ 零 toast（页签是结构行为，判据沿用 islandTabTarget）', async () => {
    const { root, cleanup } = await mount('N')
    const li = root.querySelector<HTMLElement>('.layui-tab-title > li')
    expect(li, '证据件里应当有页签标题').not.toBeNull()
    // 前置哨兵：祖先里确实有 lay-filter（点页签会命中容器）——否则「不弹」恒真。
    expect(li!.closest('[lay-filter]')).not.toBeNull()
    li!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.msg).not.toHaveBeenCalled()
    cleanup()
  })
})

/* ─────────────── 卸载 ─────────────── */

describe('卸载', () => {
  it('清空 root', async () => {
    const { root, cleanup } = await mount('N')
    cleanup()
    expect(root.innerHTML).toBe('')
  })
})
