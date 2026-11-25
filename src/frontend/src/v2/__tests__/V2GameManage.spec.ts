// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
/** 「游戏管理」v2（V2GameManage）的判据：列**恰好集合**（5 列含 v2 新显的「游戏Id」）、
 * 行内排序载荷钉值（PATCH 单字段 / 空值按 0 / 未变更不发 / 非整数拦截）、删除确认**四段逐字**
 * （条件计数段仅 >0 出现）、新增游戏弹层 = 597 款游戏库 + 27 字母片 + 搜索高亮（文案逐字）、
 * 换图标**三步直传端点序列**（ticket → PUT 原始字节（不走共享 axios）→ claim）、已删除层带
 * `deleted: 1` 取数 + PATCH `{deleted_at: null}` 恢复、权限门 fail-closed。全部夹具合成值。 */
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ElSelect } from 'element-plus'

const listConfig = vi.fn()
const createConfig = vi.fn()
const updateConfig = vi.fn()
const removeConfig = vi.fn()

vi.mock('@/api/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/config')>()),
  listConfig: (...args: unknown[]) => listConfig(...args),
  createConfig: (...args: unknown[]) => createConfig(...args),
  updateConfig: (...args: unknown[]) => updateConfig(...args),
  removeConfig: (...args: unknown[]) => removeConfig(...args),
}))

const httpRequest = vi.fn()

vi.mock('@/api/http', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/http')>()),
  request: (...args: unknown[]) => httpRequest(...args),
}))

const putFileToStorage = vi.fn()

vi.mock('@/layui-island/directUpload', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/layui-island/directUpload')>()),
  putFileToStorage: (...args: unknown[]) => putFileToStorage(...args),
}))

vi.mock('@/api/gameLibrary', () => ({
  fetchGameLibrary: vi.fn(async () => []),
}))

import V2GameManage from '../V2GameManage.vue'
import { GAME_LIBRARY } from '@/config/game-library'
import { fetchGameLibrary } from '@/api/gameLibrary'
import { useSessionStore } from '@/stores/session'

/* ── 合成夹具（GameSerializer 形状；全部合成值）────────────────────────────────── */

const GAME_A = {
  id: 11,
  name: '示例游戏甲',
  sort_order: 1,
  is_active: true,
  created_at: '2026-10-01T00:00:00+08:00',
  logo_url: '',
  deleted_at: null,
  order_count: 5,
  category_count: 2,
  project_count: 3,
}
const GAME_B = {
  ...GAME_A,
  id: 12,
  name: '示例游戏乙',
  sort_order: 2,
  logo_url: 'https://logo.example/gb.png',
  order_count: 0,
  category_count: 0,
  project_count: 0,
}
const TWO_GAMES = { items: [GAME_A, GAME_B], total: 2, page: 1, page_size: 200 }
const DELETED_ROW = { id: 21, name: '旧游戏甲', deleted_at: '2026-10-01T09:30:00+08:00' }

function makeRouter(): Router {
  const Blank = { template: '<div />' }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', name: 'v2', component: Blank, children: [{ path: '', name: 'v2-home', component: Blank }] },
      { path: '/game-config/games', name: 'game-config-games', component: Blank },
    ],
  })
}

async function mountPage(perms: string[] = ['game.server']) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const session = useSessionStore()
  session.user = {
    id: 9,
    username: 'tester_a',
    display_name: '测试甲',
    identity_plane: 'tenant',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: ['客服'],
    role: '客服',
    permissions: [],
    menu_permissions: perms,
    tenant_name: '示例工作室甲',
  }
  session.status = 'ready'
  const router = makeRouter()
  await router.push('/v2')
  await router.isReady()
  const wrapper = mount(V2GameManage, { global: { plugins: [router, pinia] } })
  await flushPromises()
  return { wrapper, router }
}

function rowOf(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], id: number) {
  return wrapper.find(`[data-v2-row="${id}"]`)
}

beforeEach(() => {
  vi.clearAllMocks()
  listConfig.mockResolvedValue(TWO_GAMES)
  httpRequest.mockResolvedValue({ key: 'k-1', upload_url: 'https://cos.example/u1' })
  vi.mocked(fetchGameLibrary).mockResolvedValue(
    GAME_LIBRARY.map(([first_key, name], index) => ({
      id: index + 1, name, first_key, sort_order: GAME_LIBRARY.length - index,
    })),
  )
})

describe('V2GameManage：权限门（fail-closed）', () => {
  it('无 game.server ⇒ 只画「无权限查看本页」，不画工具条、不发请求', async () => {
    const denied = await mountPage(['my_orders'])
    expect(denied.wrapper.find('[data-v2="no-permission"]').text()).toBe('无权限查看本页')
    expect(denied.wrapper.find('[data-v2="toolbar"]').exists()).toBe(false)
    expect(listConfig).not.toHaveBeenCalled()
  })

  it('有节点 ⇒ 工具条在，挂载即取数一次', async () => {
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="no-permission"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="toolbar"]').exists()).toBe(true)
    expect(listConfig).toHaveBeenCalledTimes(1)
    expect(listConfig).toHaveBeenCalledWith('games', { page: 1, page_size: 200 })
  })
})

describe('V2GameManage：表格与工具条', () => {
  it('列恰好集合：游戏Id / 游戏名称 / 图标 / 游戏排序(数据越大排序越靠前) / 操作', async () => {
    const { wrapper } = await mountPage()
    const headers = wrapper.findAll('.cv2-table thead th').map((th) => th.text())
    expect(headers).toEqual(['游戏Id', '游戏名称', '图标', '游戏排序(数据越大排序越靠前)', '操作'])
  })

  it('行渲染钉值：Id 右对齐等宽格、名称纯文本、无 logo ⇒ 首字色块、有 logo ⇒ 图、计数句', async () => {
    const { wrapper } = await mountPage()
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(2)
    expect(wrapper.find('[data-v2="count"]').text()).toBe('共 2 款游戏')
    const rowA = rowOf(wrapper, 11)
    expect(rowA.find(`[data-v2-game-id="11"]`).text()).toBe('11')
    expect(rowA.find(`[data-v2-game-id="11"]`).classes()).toContain('cv2-td-num')
    // 旧页蓝链死代码不搬：名称格是纯文本、无链接
    expect(rowA.find(`[data-v2-game-name="11"]`).find('a').exists()).toBe(false)
    expect(rowA.find('.gm-logo-initial').text()).toBe('示')
    expect(rowA.find('.gm-logo-img').exists()).toBe(false)
    const rowB = rowOf(wrapper, 12)
    expect(rowB.find('.gm-logo-img').attributes('src')).toBe('https://logo.example/gb.png')
    expect(rowB.find('[data-v2-relogo="12"]').text()).toBe('换图标')
    expect(rowB.find('[data-v2-del="12"]').classes()).toContain('cv2-actlink--danger')
    expect(rowB.find('[data-v2-del="12"]').text()).toBe('删除游戏')
  })

  it('截断提示逐字：数据太多，只显示前 N 条（共 M 条）', async () => {
    listConfig.mockReset()
    listConfig
      .mockResolvedValueOnce({ items: [GAME_A, GAME_B], total: 4, page: 1, page_size: 200 })
      .mockResolvedValueOnce({ items: [{ ...GAME_A, id: 13 }], total: 4, page: 2, page_size: 200 })
      .mockResolvedValue({ items: [], total: 4, page: 3, page_size: 200 })
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('数据太多，只显示前 3 条（共 4 条）')
    expect(wrapper.find('[data-v2="count"]').text()).toBe('共 4 款游戏')
  })

  it('刷新游戏列表（tool 档）⇒ 重取 + toast「已刷新」', async () => {
    const { wrapper } = await mountPage()
    const refresh = wrapper.find('[data-v2="refresh"]')
    expect(refresh.classes()).toContain('cv2-btn--tool')
    listConfig.mockClear()
    await refresh.trigger('click')
    await flushPromises()
    expect(listConfig).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('已刷新')
  })
})

describe('V2GameManage：行内排序', () => {
  it('改成 7 ⇒ PATCH 单字段 {sort_order:7} + toast「排序已保存」+ 重取', async () => {
    const { wrapper } = await mountPage()
    const input = wrapper.find('[data-v2-sort-input="11"]')
    await input.setValue('7')
    await input.trigger('change')
    await flushPromises()
    expect(updateConfig).toHaveBeenCalledTimes(1)
    expect(updateConfig).toHaveBeenCalledWith('games', 11, { sort_order: 7 })
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('排序已保存')
    expect(listConfig).toHaveBeenCalledTimes(2)
  })

  it('值未变更不发请求（有意差异：旧页不判变更）', async () => {
    const { wrapper } = await mountPage()
    const input = wrapper.find('[data-v2-sort-input="11"]')
    await input.setValue('1')
    await input.trigger('change')
    await flushPromises()
    expect(updateConfig).not.toHaveBeenCalled()
  })

  it('空值按 0 提交', async () => {
    const { wrapper } = await mountPage()
    const input = wrapper.find('[data-v2-sort-input="12"]')
    await input.setValue('')
    await input.trigger('change')
    await flushPromises()
    expect(updateConfig).toHaveBeenCalledWith('games', 12, { sort_order: 0 })
  })

  it('非整数拦截：toast「排序值只能是数字」+ 重取该格（PATCH 不发）', async () => {
    const { wrapper } = await mountPage()
    const input = wrapper.find('[data-v2-sort-input="11"]')
    await input.setValue('3.5')
    await input.trigger('change')
    await flushPromises()
    expect(updateConfig).not.toHaveBeenCalled()
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('排序值只能是数字')
    expect(listConfig).toHaveBeenCalledTimes(2)
  })

  it('保存失败 ⇒ 后端原文 + 重取（不留假象）', async () => {
    updateConfig.mockRejectedValueOnce(new Error('排序值超出范围'))
    const { wrapper } = await mountPage()
    const input = wrapper.find('[data-v2-sort-input="11"]')
    await input.setValue('9')
    await input.trigger('change')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('排序值超出范围')
    expect(wrapper.find('[data-v2="toast"]').classes()).toContain('cv2-toast--error')
    expect(listConfig).toHaveBeenCalledTimes(2)
  })
})

describe('V2GameManage：删除确认（四段逐字，照 island deleteWarning L172-184）', () => {
  const SEG_WITH_COUNTS = [
    '确定将游戏删除吗？',
    '该游戏下面有 2 个项目分类、3 个常用项目、5 笔订单。',
    '删除后它会（连同下面的分类与项目）从录单和各页下拉里消失；',
    '历史订单不受影响，以后可在工具栏的「已删除」里恢复。',
  ]

  it('有引用计数 ⇒ 四段全出（仅 >0 项进计数段、顿号连接带句号）', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, 11).find('[data-v2-del="11"]').trigger('click')
    const segs = wrapper.findAll('[data-v2="del-seg"]').map((p) => p.text())
    expect(segs).toEqual(SEG_WITH_COUNTS)
  })

  it('计数全 0 ⇒ 条件计数段不出（共三段）', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, 12).find('[data-v2-del="12"]').trigger('click')
    const segs = wrapper.findAll('[data-v2="del-seg"]').map((p) => p.text())
    expect(segs).toEqual([SEG_WITH_COUNTS[0], SEG_WITH_COUNTS[2], SEG_WITH_COUNTS[3]])
  })

  it('确认 ⇒ DELETE + toast「删除成功」+ 重取；失败 ⇒ 后端原文 + 重取', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, 11).find('[data-v2-del="11"]').trigger('click')
    await wrapper.find('[data-v2="confirm-del"]').trigger('click')
    await flushPromises()
    expect(removeConfig).toHaveBeenCalledWith('games', 11)
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('删除成功')
    expect(listConfig).toHaveBeenCalledTimes(2)

    removeConfig.mockClear()
    removeConfig.mockRejectedValueOnce(new Error('该游戏已被订单引用，不能删除'))
    await rowOf(wrapper, 12).find('[data-v2-del="12"]').trigger('click')
    await wrapper.find('[data-v2="confirm-del"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('该游戏已被订单引用，不能删除')
    expect(listConfig).toHaveBeenCalledTimes(3)
  })
})

describe('V2GameManage：新增一款游戏弹层（597 款游戏库，⛔ 不是自由填名）', () => {
  async function openPicker(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']) {
    await wrapper.find('[data-v2="add"]').trigger('click')
    await flushPromises()
  }

  it('结构恰好：27 字母片（默认点亮「全部」）+ 597 radio + 搜索下拉与搜索钮 + 橙色引文块', async () => {
    const { wrapper } = await mountPage()
    await openPicker(wrapper)
    expect(GAME_LIBRARY).toHaveLength(597)
    expect(wrapper.findAll('[data-v2="pick-chars"] [data-v2-char]')).toHaveLength(27)
    expect(wrapper.find('[data-v2-char="全部"]').classes()).toContain('gm-char--on')
    expect(wrapper.findAll('[data-v2="pick-list"] [data-v2-game-radio]')).toHaveLength(597)
    expect(wrapper.findAllComponents(ElSelect).some((c) => c.attributes('data-v2') === 'pick-search')).toBe(true)
    expect(wrapper.find('[data-v2="pick-search-btn"]').text()).toBe('搜索')
    expect(wrapper.find('[data-v2="pick-quote"]').text()).toBe(
      '如果列表里没有找到需要添加的游戏，可以联系团团通客服或技术添加',
    )
    expect(wrapper.find('[data-v2="pick-submit"]').text()).toBe('确定添加到本系统')
    expect(wrapper.find('[data-v2="pick-submit"]').classes()).toContain('cv2-btn--submit')
    // 脏数据照抄不修：8 项 firstKey 是 * 或 b
    const dirty = wrapper.findAll('[data-v2="pick-list"] [data-firstkey]').filter(
      (el) => ['*', 'b'].includes(el.attributes('data-firstkey') ?? ''),
    )
    expect(dirty).toHaveLength(8)
  })

  it('租户库里有数据时优先用数据库游戏库，不再受内置清单限制', async () => {
    vi.mocked(fetchGameLibrary).mockResolvedValueOnce([
      { id: 1, name: '蓝色星原：旅谣', first_key: 'L', sort_order: 1 },
    ])
    const { wrapper } = await mountPage()
    await flushPromises()
    await openPicker(wrapper)
    const radios = wrapper.findAll('[data-v2="pick-list"] [data-v2-game-radio]')
    expect(radios).toHaveLength(1)
    expect(radios[0]!.text()).toContain('蓝色星原：旅谣')
  })

  it('游戏库接口失败 ⇒ 不展示旧内置清单，并给出可见提示', async () => {
    vi.mocked(fetchGameLibrary).mockRejectedValueOnce(new Error('boom'))
    const { wrapper } = await mountPage()
    await openPicker(wrapper)
    expect(wrapper.findAll('[data-v2="pick-list"] [data-v2-game-radio]')).toHaveLength(0)
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('游戏库加载失败')
  })

  it('未选提交 ⇒ toast「请选择要添加的游戏」，零请求', async () => {
    const { wrapper } = await mountPage()
    await openPicker(wrapper)
    await wrapper.find('[data-v2="pick-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('请选择要添加的游戏')
    expect(createConfig).not.toHaveBeenCalled()
  })

  it('选中后提交 ⇒ POST {name} + toast「新增成功」+ 重取', async () => {
    const { wrapper } = await mountPage()
    await openPicker(wrapper)
    await wrapper.find('[data-v2-game-radio="三角洲行动"] input').setValue(true)
    await wrapper.find('[data-v2="pick-submit"]').trigger('click')
    await flushPromises()
    expect(createConfig).toHaveBeenCalledWith('games', { name: '三角洲行动' })
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('新增成功')
    expect(listConfig).toHaveBeenCalledTimes(2)
  })

  it('失败不关层（可改选重试）：toast 后端原文', async () => {
    createConfig.mockRejectedValueOnce(new Error('该游戏已存在'))
    const { wrapper } = await mountPage()
    await openPicker(wrapper)
    await wrapper.find('[data-v2-game-radio="三角洲行动"] input').setValue(true)
    await wrapper.find('[data-v2="pick-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').classes()).toContain('cv2-toast--error')
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('该游戏已存在')
  })

  it('点字母片 S ⇒ firstKey=S 的项橙色高亮；点「全部」⇒ 高亮全清（脏数据 8 项永远点不亮）', async () => {
    const { wrapper } = await mountPage()
    await openPicker(wrapper)
    await wrapper.find('[data-v2-char="S"]').trigger('click')
    const lit = wrapper.findAll('.gm-hit-char')
    expect(lit.length).toBeGreaterThan(0)
    for (const el of lit) {
      expect(['S']).toContain(el.attributes('data-firstkey'))
    }
    await wrapper.find('[data-v2-char="全部"]').trigger('click')
    expect(wrapper.findAll('.gm-hit-char')).toHaveLength(0)
  })

  it('下拉选中 ⇒ 同名项红色高亮（清单里「卡厄思梦境」两处）+ toast「已找到，请注意下方红色字体」', async () => {
    const { wrapper } = await mountPage()
    await openPicker(wrapper)
    const sel = wrapper
      .findAllComponents(ElSelect)
      .find((c) => c.attributes('data-v2') === 'pick-search')!
    sel.vm.$emit('update:modelValue', '卡厄思梦境')
    sel.vm.$emit('change', '卡厄思梦境')
    await flushPromises()
    expect(wrapper.findAll('.gm-hit-search')).toHaveLength(2)
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('已找到，请注意下方红色字体')
  })

  it('「搜索」钮 = 同一高亮动作但不弹「已找到」（旧页死按钮接活的设计稿口径）', async () => {
    const { wrapper } = await mountPage()
    await openPicker(wrapper)
    const sel = wrapper
      .findAllComponents(ElSelect)
      .find((c) => c.attributes('data-v2') === 'pick-search')!
    sel.vm.$emit('update:modelValue', '王者荣耀')
    await flushPromises()
    await wrapper.find('[data-v2="pick-search-btn"]').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.gm-hit-search')).toHaveLength(1)
    // 搜索钮只高亮、不弹「已找到」（toast 不存在）
    expect(wrapper.find('[data-v2="toast"]').exists()).toBe(false)
  })
})

describe('V2GameManage：换图标（三步直传，PUT 不走共享 axios）', () => {
  async function pickLogo(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], id: number) {
    await rowOf(wrapper, id).find(`[data-v2-relogo="${id}"]`).trigger('click')
    const input = wrapper.find('[data-v2="logo-file"]')
    expect(input.exists()).toBe(true)
    expect(input.attributes('accept')).toBe('image/jpeg,image/png')
    const file = new File(['fake-bytes'], 'logo.png', { type: 'image/png' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await flushPromises()
  }

  it('端点序列：①ticket {ext 不带点} → ②putFileToStorage 原始字节 → ③claim {key}，成功 toast「图标已更新」+ 重取', async () => {
    const { wrapper } = await mountPage()
    await pickLogo(wrapper, 11)
    expect(httpRequest).toHaveBeenNthCalledWith(1, {
      method: 'post',
      url: '/api/business/games/11/logo/ticket',
      data: { ext: 'png' },
    })
    expect(putFileToStorage).toHaveBeenCalledTimes(1)
    expect(putFileToStorage).toHaveBeenCalledWith('https://cos.example/u1', expect.any(File))
    expect(httpRequest).toHaveBeenNthCalledWith(2, {
      method: 'post',
      url: '/api/business/games/11/logo',
      data: { key: 'k-1' },
    })
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('图标已更新')
    expect(listConfig).toHaveBeenCalledTimes(2)
  })

  it('扩展名无点 ⇒ 交空串让后端判；后端拒 ⇒ 后端原文 + 不发生 PUT/claim + 重取', async () => {
    httpRequest.mockRejectedValueOnce(new Error('只支持 jpg / jpeg / png'))
    const { wrapper } = await mountPage()
    await rowOf(wrapper, 11).find('[data-v2-relogo="11"]').trigger('click')
    const input = wrapper.find('[data-v2="logo-file"]')
    const file = new File(['x'], 'noext', { type: '' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await flushPromises()
    expect(httpRequest).toHaveBeenCalledWith(
      expect.objectContaining({ url: '/api/business/games/11/logo/ticket', data: { ext: '' } }),
    )
    expect(putFileToStorage).not.toHaveBeenCalled()
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('只支持 jpg / jpeg / png')
    expect(listConfig).toHaveBeenCalledTimes(2)
  })
})

describe('V2GameManage：已删除弹层', () => {
  it('取数带 deleted: 1；行渲染 名称/删除时间/恢复', async () => {
    listConfig.mockClear()
    listConfig
      .mockResolvedValueOnce(TWO_GAMES)
      .mockResolvedValue({ items: [DELETED_ROW], total: 1, page: 1, page_size: 200 })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="deleted"]').trigger('click')
    await flushPromises()
    expect(listConfig).toHaveBeenCalledWith('games', { deleted: 1, page: 1, page_size: 200 })
    const row = wrapper.find('[data-v2-deleted-row="21"]')
    expect(row.exists()).toBe(true)
    expect(row.text()).toContain('旧游戏甲')
    expect(row.text()).toContain('2026-10-01 09:30')
    expect(row.find('[data-v2-restore="21"]').text()).toBe('恢复')
  })

  it('恢复 ⇒ PATCH {deleted_at: null} + toast「已恢复」+ 重取本层与主表', async () => {
    listConfig.mockClear()
    listConfig
      .mockResolvedValueOnce(TWO_GAMES)
      .mockResolvedValue({ items: [DELETED_ROW], total: 1, page: 1, page_size: 200 })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="deleted"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-v2-restore="21"]').trigger('click')
    await flushPromises()
    expect(updateConfig).toHaveBeenCalledWith('games', 21, { deleted_at: null })
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('已恢复')
    // mount(1) + 开层(1) + 恢复后本层(1) + 主表(1)
    expect(listConfig).toHaveBeenCalledTimes(4)
  })

  it('空列表 ⇒ 「没有已删除的记录」', async () => {
    listConfig.mockClear()
    listConfig
      .mockResolvedValueOnce(TWO_GAMES)
      .mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="deleted"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="deleted-empty"]').text()).toBe('没有已删除的记录')
  })
})
