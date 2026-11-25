// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
/** 检查分类折叠、权限及分类和常用项目的操作。 */
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ElOption, ElSelect } from 'element-plus'

const fetchAllGames = vi.fn()
const listConfig = vi.fn()
const createConfig = vi.fn()
const updateConfig = vi.fn()
const removeConfig = vi.fn()

vi.mock('@/api/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/config')>()),
  fetchAllGames: (...args: unknown[]) => fetchAllGames(...args),
  listConfig: (...args: unknown[]) => listConfig(...args),
  createConfig: (...args: unknown[]) => createConfig(...args),
  updateConfig: (...args: unknown[]) => updateConfig(...args),
  removeConfig: (...args: unknown[]) => removeConfig(...args),
}))

import V2GameProjects from '../V2GameProjects.vue'
import { useSessionStore } from '@/stores/session'

/* ── 合成夹具（CategoryRow / ProjectRow 形状；全部合成值）────────────────────── */

const GAMES = [
  { id: 2, name: '示例游戏一', sort_order: 1, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
  { id: 3, name: '示例游戏二', sort_order: 2, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
]
const CAT_A = {
  id: 4, name: '分类甲', game: 2, sort_order: 1, is_active: true,
  created_at: '2026-10-01T00:00:00+08:00', deleted_at: null, order_count: 6,
  assigned_platforms: '8,10', title_prefix: '【', title_suffix: '】', project_count: 2,
}
const CAT_B = { ...CAT_A, id: 5, name: '分类乙', sort_order: 2, assigned_platforms: null, title_prefix: null, title_suffix: null, project_count: 0 }
const PROJ_A = {
  id: 9, name: '项目甲', category: 4, sort_order: 1, is_active: true,
  created_at: '2026-10-01T00:00:00+08:00', deleted_at: null, order_count: 3, base_price: null,
}
const PROJ_B = { ...PROJ_A, id: 10, name: '项目乙', sort_order: 2, order_count: 0 }
const PAGE = { page: 1, page_size: 200 }

function makeRouter(): Router {
  const Blank = { template: '<div />' }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', name: 'v2', component: Blank, children: [{ path: '', name: 'v2-home', component: Blank }] },
      { path: '/game-config/projects', name: 'game-config-projects', component: Blank },
    ],
  })
}

async function mountPage(perms: string[] = ['game.order_type_categories', 'game.common_projects'], expand = true) {
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
  const wrapper = mount(V2GameProjects, { global: { plugins: [router, pinia] } })
  await flushPromises()
  if (expand && wrapper.find('[data-v2-cat-toggle="4"]').exists()) {
    await wrapper.find('[data-v2-cat-toggle="4"]').trigger('click')
    await flushPromises()
  }
  return { wrapper, router }
}

function gameSelectOf(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']) {
  const sel = wrapper.findAllComponents(ElSelect).find((c) => c.attributes('data-v2-filter') === 'game')
  expect(sel, '应存在 data-v2-filter="game" 的游戏下拉').toBeDefined()
  return sel!
}

beforeEach(() => {
  vi.clearAllMocks()
  fetchAllGames.mockResolvedValue(GAMES)
  listConfig.mockImplementation((resource: string) => {
    if (resource === 'categories') {
      return Promise.resolve({ items: [CAT_A, CAT_B], total: 2, ...PAGE })
    }
    return Promise.resolve({ items: [PROJ_A, PROJ_B], total: 2, ...PAGE })
  })
})

describe('V2GameProjects：布局与首屏渲染', () => {
  it('游戏下拉默认第一款（categories 按 {game:2} 取数）；无「全部」项', async () => {
    const { wrapper } = await mountPage()
    expect(listConfig).toHaveBeenCalledWith('categories', { game: 2, ...PAGE })
    expect(wrapper.text()).not.toContain('全部游戏')
    expect(wrapper.text()).not.toContain('全部')
  })

  it('默认折叠，点分类展开项目，再点一次收起', async () => {
    const { wrapper } = await mountPage(undefined, false)
    expect(wrapper.find('[data-v2-cat-toggle="4"]').attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('[data-v2-proj-row="9"]').exists()).toBe(false)
    expect(listConfig).not.toHaveBeenCalledWith('projects', expect.anything())
    await wrapper.find('[data-v2-cat-toggle="4"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2-cat-toggle="4"]').attributes('aria-expanded')).toBe('true')
    expect(wrapper.find('[data-v2-proj-row="9"]').text()).toContain('项目甲')
    await wrapper.find('[data-v2-cat-toggle="4"]').trigger('click')
    expect(wrapper.find('[data-v2-proj-row="9"]').exists()).toBe(false)
  })

  it('切换分类会关闭之前的项目并加载所选分类', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-cat-toggle="5"]').trigger('click')
    await flushPromises()
    expect(listConfig).toHaveBeenCalledWith('projects', { category: 5, ...PAGE })
    expect(wrapper.find('[data-v2-cat-toggle="4"]').attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('[data-v2-cat-toggle="5"]').attributes('aria-expanded')).toBe('true')
  })

  it('切游戏立即隐藏旧分类，迟到的旧请求不能覆盖当前游戏', async () => {
    let finishOld!: (value: unknown) => void
    const { wrapper } = await mountPage()
    listConfig.mockImplementation((resource: string, params: { game?: number }) => {
      if (resource === 'categories' && params.game === 3) {
        return new Promise((resolve) => { finishOld = resolve })
      }
      if (resource === 'categories') return Promise.resolve({ items: [CAT_A], total: 1, ...PAGE })
      return Promise.resolve({ items: [], total: 0, ...PAGE })
    })
    const select = gameSelectOf(wrapper)
    select.vm.$emit('update:modelValue', 3)
    select.vm.$emit('change', 3)
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-v2-cat-row="4"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2-cat-edit="4"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2-cat-del="4"]').exists()).toBe(false)
    select.vm.$emit('update:modelValue', 2)
    select.vm.$emit('change', 2)
    await flushPromises()
    finishOld({ items: [{ ...CAT_B, game: 3 }], total: 1, ...PAGE })
    await flushPromises()
    expect(wrapper.find('[data-v2-cat-row="4"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2-cat-row="5"]').exists()).toBe(false)
  })

  it('快速切换分类时，较晚返回的旧结果不会覆盖新分类', async () => {
    let finishOld!: (value: unknown) => void
    listConfig.mockImplementation((resource: string, params: { category?: number }) => {
      if (resource === 'categories') return Promise.resolve({ items: [CAT_A, CAT_B], total: 2, ...PAGE })
      if (params.category === 4) return new Promise((resolve) => { finishOld = resolve })
      return Promise.resolve({ items: [{ ...PROJ_B, category: 5 }], total: 1, ...PAGE })
    })
    const { wrapper } = await mountPage(undefined, false)
    await wrapper.find('[data-v2-cat-toggle="4"]').trigger('click')
    await wrapper.find('[data-v2-cat-toggle="5"]').trigger('click')
    await flushPromises()
    finishOld({ items: [PROJ_A], total: 1, ...PAGE })
    await flushPromises()
    expect(wrapper.find('[data-v2-proj-row="10"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2-proj-row="9"]').exists()).toBe(false)
  })

  it('换游戏 ⇒ 分类按新游戏重取且选中复位；刷新数据 ⇒ 重取字典（保住选中）+ toast「已刷新」', async () => {
    const { wrapper } = await mountPage()
    const sel = gameSelectOf(wrapper)
    sel.vm.$emit('update:modelValue', 3)
    sel.vm.$emit('change', 3)
    await flushPromises()
    expect(listConfig).toHaveBeenCalledWith('categories', { game: 3, ...PAGE })
    expect(wrapper.find('[data-v2-proj-row="9"]').exists()).toBe(false)

    fetchAllGames.mockClear()
    await wrapper.find('[data-v2="refresh"]').trigger('click')
    await flushPromises()
    expect(fetchAllGames).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('已刷新')
    expect(listConfig).toHaveBeenCalledWith('categories', { game: 2, ...PAGE })
    expect(wrapper.find('[data-v2-cat-toggle="4"]').attributes('aria-expanded')).toBe('false')
  })
})

describe('V2GameProjects：权限分收（fail-closed）', () => {
  it('只有 game.common_projects ⇒ 分类侧无写入口（名称非链接、操作列 —），项目侧齐全', async () => {
    const { wrapper } = await mountPage(['game.common_projects'])
    expect(wrapper.find('[data-v2="add-category"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="deleted-categories"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="add-project"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2="deleted-projects"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2-cat-edit="4"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2-cat-del="4"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2-cat-edit="4"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2-proj-edit="9"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2-proj-del="9"]').exists()).toBe(true)
  })

  it('两节点全无 + 空列表 ⇒ 左右面板各出「无权限」空态；所有写入口不画', async () => {
    listConfig.mockImplementation(() => Promise.resolve({ items: [], total: 0, ...PAGE }))
    const { wrapper } = await mountPage([])
    expect(wrapper.find('[data-v2="add-category"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="add-project"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="cat-empty"]').text()).toBe('无权限管理项目分类')
    expect(wrapper.find('[data-v2="proj-panel"]').exists()).toBe(false)
  })

  it('两节点齐全 ⇒ 四颗动作钮 + 行内编辑/删除都在', async () => {
    const { wrapper } = await mountPage()
    for (const anchor of ['add-category', 'add-project', 'deleted-categories', 'deleted-projects']) {
      expect(wrapper.find(`[data-v2="${anchor}"]`).exists()).toBe(true)
    }
    expect(wrapper.find('[data-v2-cat-del="4"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2-proj-del="9"]').exists()).toBe(true)
  })
})

describe('V2GameProjects：分类弹层', () => {
  async function openNew(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']) {
    await wrapper.find('[data-v2="add-category"]').trigger('click')
    await flushPromises()
  }

  it('新增分类只显示名称和排序，保留排序说明', async () => {
    const { wrapper } = await mountPage()
    await openNew(wrapper)
    const dlg = wrapper.find('[data-v2="dlg-category"]')
    expect(dlg.find('.el-dialog__title').text()).toBe('新增项目分类')
    expect(dlg.findAll('[data-v2-platform]')).toHaveLength(0)
    expect(dlg.text()).toContain('数字越大越靠前')
    expect(dlg.find('[data-v2-field="name"]').attributes('maxlength')).toBe('64')
    expect(dlg.find('[data-v2-field="sort_order"]').attributes('maxlength')).toBe('9')
    expect(dlg.find('[data-v2-field="title_prefix"]').exists()).toBe(false)
    expect(dlg.find('[data-v2-field="title_suffix"]').exists()).toBe(false)
  })

  async function saveExpectToast(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], message: string) {
    await wrapper.find('[data-v2="category-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toBe(message)
    expect(createConfig).not.toHaveBeenCalled()
  }

  it('校验逐字：分类名称不能为空', async () => {
    const { wrapper } = await mountPage()
    await openNew(wrapper)
    await wrapper.find('[data-v2="dlg-category"] [data-v2-field="name"]').setValue('')
    await saveExpectToast(wrapper, '分类名称不能为空')
    // 校验失败不落请求也不关层。
    expect(wrapper.find('[data-v2="dlg-category"]').exists()).toBe(true)
  })

  it('校验逐字：排序值不能为空，哪怕写个数字0', async () => {
    const { wrapper } = await mountPage()
    await openNew(wrapper)
    await wrapper.find('[data-v2="dlg-category"] [data-v2-field="name"]').setValue('分类丙')
    await wrapper.find('[data-v2="dlg-category"] [data-v2-field="sort_order"]').setValue('')
    await saveExpectToast(wrapper, '排序值不能为空，哪怕写个数字0')
    expect(wrapper.find('[data-v2="dlg-category"]').exists()).toBe(true)
  })

  it('校验逐字：排序值只能是数字', async () => {
    const { wrapper } = await mountPage()
    await openNew(wrapper)
    await wrapper.find('[data-v2="dlg-category"] [data-v2-field="name"]').setValue('分类丙')
    await wrapper.find('[data-v2="dlg-category"] [data-v2-field="sort_order"]').setValue('3.5')
    await saveExpectToast(wrapper, '排序值只能是数字')
    expect(wrapper.find('[data-v2="dlg-category"]').exists()).toBe(true)
  })

  it('新增保存使用空的平台和标题设置，带当前游戏', async () => {
    createConfig.mockResolvedValue({ ...CAT_A, id: 6, name: '分类丙' })
    const { wrapper } = await mountPage()
    await openNew(wrapper)
    await wrapper.find('[data-v2="dlg-category"] [data-v2-field="name"]').setValue('分类丙')
    await wrapper.find('[data-v2="dlg-category"] [data-v2-field="sort_order"]').setValue('3')
    await wrapper.find('[data-v2="category-save"]').trigger('click')
    await flushPromises()
    expect(createConfig).toHaveBeenCalledWith('categories', {
      name: '分类丙',
      sort_order: 3,
      title_prefix: '',
      title_suffix: '',
      assigned_platforms: '',
      game: 2,
    })
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('新增成功')
    expect(listConfig).toHaveBeenCalledWith('categories', { game: 2, ...PAGE })
  })

  it('编辑回填（名称/排序/标题前后缀/平台勾选）；保存 ⇒ PATCH 载荷**不带 game 键**', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-cat-edit="4"]').trigger('click')
    await flushPromises()
    const dlg = wrapper.find('[data-v2="dlg-category"]')
    expect(dlg.find('.el-dialog__title').text()).toBe('编辑分类：分类甲')
    expect((dlg.find('[data-v2-field="name"]').element as HTMLInputElement).value).toBe('分类甲')
    expect((dlg.find('[data-v2-field="sort_order"]').element as HTMLInputElement).value).toBe('1')
    expect((dlg.find('[data-v2-field="title_prefix"]').element as HTMLInputElement).value).toBe('【')
    expect((dlg.find('[data-v2-platform="8"] input').element as HTMLInputElement).checked).toBe(true)
    expect((dlg.find('[data-v2-platform="30"] input').element as HTMLInputElement).checked).toBe(false)
    await wrapper.find('[data-v2="category-save"]').trigger('click')
    await flushPromises()
    expect(updateConfig).toHaveBeenCalledTimes(1)
    expect(updateConfig).toHaveBeenCalledWith('categories', 4, {
      name: '分类甲',
      sort_order: 1,
      title_prefix: '【',
      title_suffix: '】',
      assigned_platforms: '8,10',
    })
  })
})

describe('V2GameProjects：项目弹层', () => {
  async function openNew(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']) {
    await wrapper.find('[data-v2="add-project"]').trigger('click')
    await flushPromises()
  }

  it('标题「新增常用代练项目」；分类选项文案「名称（N 个项目）」；name maxlength=450', async () => {
    const { wrapper } = await mountPage()
    await openNew(wrapper)
    const dlg = wrapper.find('[data-v2="dlg-project"]')
    expect(dlg.find('.el-dialog__title').text()).toBe('新增常用代练项目')
    expect(dlg.findAll('.gp-form-field .cv2-field-label').map((label) => label.text())).toEqual(['所属分类', '项目名称', '排序'])
    // 选项文案不依赖下拉展开：直接钉 ElOption 的 label props
    const labels = wrapper.findAllComponents(ElOption).map((o) => String(o.props('label')))
    expect(labels).toContain('分类甲（2 个项目）')
    expect(labels).toContain('分类乙（0 个项目）')
    expect(dlg.find('[data-v2-field="name"]').attributes('maxlength')).toBe('450')
  })

  it('新增保存 ⇒ 默认分类 = 当前选中分类（不碰下拉直接保存，category=4）', async () => {
    const { wrapper } = await mountPage()
    await openNew(wrapper)
    await wrapper.find('[data-v2="dlg-project"] [data-v2-field="name"]').setValue('项目丙')
    await wrapper.find('[data-v2="project-save"]').trigger('click')
    await flushPromises()
    expect(createConfig).toHaveBeenCalledWith('projects', { name: '项目丙', sort_order: 0, category: 4 })
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('新增成功')
  })

  it('在展开的分类中新增项目时，默认归到该分类', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-cat-toggle="5"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-v2-add-project-for="5"]').trigger('click')
    await wrapper.find('[data-v2="dlg-project"] [data-v2-field="name"]').setValue('项目丙')
    await wrapper.find('[data-v2="project-save"]').trigger('click')
    await flushPromises()
    expect(createConfig).toHaveBeenCalledWith('projects', { name: '项目丙', sort_order: 0, category: 5 })
  })

  it('校验逐字（顺序照旧页：名称 → 排序空 → 排序数字 → 分类必选）', async () => {
    const { wrapper } = await mountPage()
    await openNew(wrapper)
    await wrapper.find('[data-v2="project-save"]').trigger('click')
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('项目名称不能为空')

    await wrapper.find('[data-v2="dlg-project"] [data-v2-field="name"]').setValue('项目丙')
    await wrapper.find('[data-v2="dlg-project"] [data-v2-field="sort_order"]').setValue('')
    await wrapper.find('[data-v2="project-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('排序值不能为空，哪怕写个数字0')

    await wrapper.find('[data-v2="dlg-project"] [data-v2-field="sort_order"]').setValue('x')
    await wrapper.find('[data-v2="project-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('排序值只能是数字')

    await wrapper.find('[data-v2="dlg-project"] [data-v2-field="sort_order"]').setValue('1')
    const sel = wrapper
      .findAllComponents(ElSelect)
      .find((c) => c.attributes('data-v2-field') === 'category')!
    sel.vm.$emit('update:modelValue', null)
    sel.vm.$emit('change', null)
    await flushPromises()
    await wrapper.find('[data-v2="project-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('请选择项目分类')
    expect(createConfig).not.toHaveBeenCalled()
  })

  it('编辑项目改选分类 ⇒ PATCH {name, sort_order, category:5}；右侧列表跟随新分类重取', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-proj-edit="9"]').trigger('click')
    await flushPromises()
    const dlg = wrapper.find('[data-v2="dlg-project"]')
    expect(dlg.find('.el-dialog__title').text()).toBe('编辑项目：项目甲')
    const sel = wrapper
      .findAllComponents(ElSelect)
      .find((c) => c.attributes('data-v2-field') === 'category')!
    sel.vm.$emit('update:modelValue', 5)
    sel.vm.$emit('change', 5)
    await flushPromises()
    await wrapper.find('[data-v2="project-save"]').trigger('click')
    await flushPromises()
    expect(updateConfig).toHaveBeenCalledWith('projects', 9, {
      name: '项目甲',
      sort_order: 1,
      category: 5,
    })
    // 旧页 L624：跟着用户刚选的分类走
    expect(wrapper.find('[data-v2-cat-toggle="5"]').attributes('aria-expanded')).toBe('true')
    expect(listConfig).toHaveBeenLastCalledWith('projects', { category: 5, ...PAGE })
  })
})

describe('V2GameProjects：删除确认（条件中段逐字）', () => {
  it('分类（project_count>0）⇒ 三段全出', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-cat-del="4"]').trigger('click')
    const segs = wrapper.findAll('[data-v2="del-seg"]').map((p) => p.text())
    expect(segs).toEqual([
      '确定删除分类「分类甲」吗？',
      '这个分类下面还有 2 个常用项目，会一起下架。',
      '历史订单不受影响，以后可在「已删除分类」里恢复。',
    ])
  })

  it('分类（project_count=0）⇒ 条件中段不出（两段）', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-cat-del="5"]').trigger('click')
    const segs = wrapper.findAll('[data-v2="del-seg"]').map((p) => p.text())
    expect(segs).toEqual([
      '确定删除分类「分类乙」吗？',
      '历史订单不受影响，以后可在「已删除分类」里恢复。',
    ])
  })

  it('项目（order_count=3）⇒ 三段逐字；确认 ⇒ DELETE + toast「删除成功」+ 重取', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-proj-del="9"]').trigger('click')
    const segs = wrapper.findAll('[data-v2="del-seg"]').map((p) => p.text())
    expect(segs).toEqual([
      '确定删除项目「项目甲」吗？',
      '这个项目被 3 笔订单引用。',
      '删除后会从录单下拉里消失，以后可在「已删除项目」里恢复。',
    ])
    listConfig.mockClear()
    await wrapper.find('[data-v2="confirm-del"]').trigger('click')
    await flushPromises()
    expect(removeConfig).toHaveBeenCalledWith('projects', 9)
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('删除成功')
    expect(listConfig).toHaveBeenCalledWith('categories', { game: 2, ...PAGE })
  })
})

describe('V2GameProjects：已删除两层', () => {
  it('已删除分类：按 {game, deleted:1} 取数；标题「已删除的项目分类」；恢复 ⇒ PATCH {deleted_at:null} + toast「已恢复」', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="deleted-categories"]').trigger('click')
    await flushPromises()
    expect(listConfig).toHaveBeenLastCalledWith('categories', { game: 2, deleted: 1, ...PAGE })
    const dlg = wrapper.find('[data-v2="dlg-deleted-categories"]')
    expect(dlg.find('.el-dialog__title').text()).toBe('已删除的项目分类')
    expect(dlg.find('[data-v2-deleted-cat-row="4"]').exists()).toBe(true)
    await dlg.find('[data-v2-restore-cat="4"]').trigger('click')
    await flushPromises()
    expect(updateConfig).toHaveBeenCalledWith('categories', 4, { deleted_at: null })
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('已恢复')
  })

  it('已删除项目：按 {game, deleted:1} 取数；标题「已删除的常用项目」', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="deleted-projects"]').trigger('click')
    await flushPromises()
    expect(listConfig).toHaveBeenLastCalledWith('projects', { game: 2, deleted: 1, ...PAGE })
    const dlg = wrapper.find('[data-v2="dlg-deleted-projects"]')
    expect(dlg.find('.el-dialog__title').text()).toBe('已删除的常用项目')
    expect(dlg.text()).toContain('删除时间')
  })
})

describe('V2GameProjects：截断与空态（三态文案逐字）', () => {
  it('分类截断 ⇒ 「分类太多，只显示了前 N 条（共 M 条）」', async () => {
    let catCalls = 0
    listConfig.mockImplementation((resource: string) => {
      if (resource === 'categories') {
        catCalls += 1
        if (catCalls === 1) return Promise.resolve({ items: [CAT_A], total: 3, ...PAGE })
        if (catCalls === 2) return Promise.resolve({ items: [CAT_B], total: 3, ...PAGE })
        return Promise.resolve({ items: [], total: 3, ...PAGE })
      }
      return Promise.resolve({ items: [], total: 0, ...PAGE })
    })
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('分类太多，只显示了前 2 条（共 3 条）')
  })

  it('项目截断 ⇒ 「项目太多，只显示了前 N 条（共 M 条）」', async () => {
    let projCalls = 0
    listConfig.mockImplementation((resource: string, _params: Record<string, unknown>) => {
      if (resource === 'categories') {
        return Promise.resolve({ items: [CAT_A, CAT_B], total: 2, ...PAGE })
      }
      projCalls += 1
      if (projCalls <= 2) return Promise.resolve({ items: projCalls === 1 ? [PROJ_A] : [PROJ_B], total: 3, ...PAGE })
      return Promise.resolve({ items: [], total: 3, ...PAGE })
    })
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('项目太多，只显示了前 2 条（共 3 条）')
  })

  it('空分类 / 空项目 / 无游戏 ⇒ 三句逐字', async () => {
    listConfig.mockImplementation(() => Promise.resolve({ items: [], total: 0, ...PAGE }))
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="cat-empty"]').text()).toBe('还没有项目分类，点上方「新增分类」')
    expect(wrapper.find('[data-v2="proj-panel"]').exists()).toBe(false)

    fetchAllGames.mockResolvedValue([])
    const none = await mountPage()
    expect(none.wrapper.find('[data-v2="cat-empty"]').text()).toBe('还没有游戏，请先到「游戏管理」新增一款游戏')
    expect(none.wrapper.find('[data-v2="toast"]').text()).toBe('还没有游戏，请先到「游戏管理」新增一款游戏')
  })
})
