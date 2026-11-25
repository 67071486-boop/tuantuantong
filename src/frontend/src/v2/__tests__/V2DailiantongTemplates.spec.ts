// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 「代练通订单要求模板」v2（V2DailiantongTemplates）的判据：mock API → **钉值渲染**
 * （定位说明逐字含两处 <b> / 4 列恰好集合 / 默认绿签与灰签 / id=null 行无写按钮 / 空标题
 * 回落「模板 <id>」）、真交互（新增 POST 载荷钉值 / 编辑回填+保存修改 PATCH 三项全发 /
 * 取消编辑复位 / 删除与设默认先过确认弹层且文案逐字 / 失败 toast+说明条两处 / busy 在途闸
 * 提交中禁用全部按钮）、状态行三态（record_count=null 省略服务端总数段）、权限门
 * fail-closed。功能语义的出处 = island **自有功能页**
 * `layui-island/pages/dailiantong-order-templates.ts`（MESSAGES / 确认句 / 定位说明逐字）；
 * 全部夹具都是**合成值**（PII 纪律）。 */
const listDailiantongOrderTemplates = vi.fn()
const createDailiantongOrderTemplate = vi.fn()
const updateDailiantongOrderTemplate = vi.fn()
const deleteDailiantongOrderTemplate = vi.fn()
const setDefaultDailiantongOrderTemplate = vi.fn()

vi.mock('@/api/dailiantongExtra', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/dailiantongExtra')>()),
  listDailiantongOrderTemplates: (...args: unknown[]) => listDailiantongOrderTemplates(...args),
  createDailiantongOrderTemplate: (...args: unknown[]) => createDailiantongOrderTemplate(...args),
  updateDailiantongOrderTemplate: (...args: unknown[]) => updateDailiantongOrderTemplate(...args),
  deleteDailiantongOrderTemplate: (...args: unknown[]) => deleteDailiantongOrderTemplate(...args),
  setDefaultDailiantongOrderTemplate: (...args: unknown[]) => setDefaultDailiantongOrderTemplate(...args),
}))

import V2DailiantongTemplates from '../V2DailiantongTemplates.vue'
import { useSessionStore } from '@/stores/session'

/* ── 合成夹具（上游 OrderRequireTemp 形状；值全部合成）────────────────────────────── */

const TPL_A = { id: 11, title: '合成模板甲', temp: '要求每天在线 8 小时（合成）', is_default: true }
const TPL_B = { id: 12, title: '合成模板乙', temp: '示例要求 B', is_default: false }
/** `id=null`（上游 ID 解析不出）⇒ 操作列画「—」、无任何写按钮。 */
const TPL_NOID = { id: null, title: '合成模板丙', temp: '示例要求 C', is_default: false }
/** 标题/内容为空的行：空格画「—」、确认句回落「模板 <id>」。 */
const TPL_EMPTY = { id: 13, title: '', temp: '', is_default: false }

const LIST_PAYLOAD = { items: [TPL_A, TPL_B, TPL_NOID, TPL_EMPTY], record_count: 7 }

function makeRouter(): Router {
  const Blank = { template: '<div />' }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', name: 'v2', component: Blank, children: [{ path: '', name: 'v2-home', component: Blank }] },
      { path: '/system/dailiantong-order-templates', name: 'dailiantong-order-templates', component: Blank },
    ],
  })
}

type Perms = { menu_permissions: string[] }

async function mountPage(perms: Perms = { menu_permissions: ['order.list'] }) {
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
    menu_permissions: perms.menu_permissions,
    tenant_name: '示例工作室甲',
  }
  session.status = 'ready'
  const router = makeRouter()
  await router.push('/v2')
  await router.isReady()
  const wrapper = mount(V2DailiantongTemplates, { global: { plugins: [router, pinia] } })
  await flushPromises()
  return { wrapper, router }
}

function rowOf(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], id: number | string) {
  return wrapper.find(`[data-v2-row="${id}"]`)
}

/** 填表单（标题/内容/是否默认）并点某个提交钮。 */
async function fillForm(
  wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'],
  values: { title?: string; content?: string; isDefault?: boolean },
): Promise<void> {
  if (values.title !== undefined) await wrapper.find('[data-v2-field="title"]').setValue(values.title)
  if (values.content !== undefined) await wrapper.find('[data-v2-field="content"]').setValue(values.content)
  if (values.isDefault !== undefined) await wrapper.find('[data-v2-field="default"] input').setValue(values.isDefault)
}

function titleValue(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']): string {
  return (wrapper.find('[data-v2-field="title"]').element as HTMLInputElement).value
}

beforeEach(() => {
  vi.clearAllMocks()
  listDailiantongOrderTemplates.mockResolvedValue({ ...LIST_PAYLOAD, items: LIST_PAYLOAD.items.map((r) => ({ ...r })) })
  createDailiantongOrderTemplate.mockResolvedValue({ ...TPL_B })
  updateDailiantongOrderTemplate.mockResolvedValue({ ...TPL_B })
  deleteDailiantongOrderTemplate.mockResolvedValue({ id: 12, deleted: true })
  setDefaultDailiantongOrderTemplate.mockResolvedValue({ ...TPL_B, is_default: true })
})

describe('V2DailiantongTemplates：权限门（fail-closed）', () => {
  it('无 order.list 节点 ⇒ 只画无权限空态（文案逐字），表单/列表不画、零请求', async () => {
    const denied = await mountPage({ menu_permissions: ['my_orders'] })
    expect(denied.wrapper.find('[data-v2="no-permission"]').text()).toBe('无权限查看代练通订单要求模板')
    expect(denied.wrapper.find('[data-v2="form-panel"]').exists()).toBe(false)
    expect(denied.wrapper.find('[data-v2="list-panel"]').exists()).toBe(false)
    expect(listDailiantongOrderTemplates).not.toHaveBeenCalled()

    const allowed = await mountPage()
    expect(allowed.wrapper.find('[data-v2="no-permission"]').exists()).toBe(false)
    expect(allowed.wrapper.find('[data-v2="form-panel"]').exists()).toBe(true)
  })
})

describe('V2DailiantongTemplates：钉值渲染', () => {
  it('页顶定位说明逐字照 island buildHtml（L170-173）原文，两处 <b> 强调随原文保留', async () => {
    const { wrapper } = await mountPage()
    const note = wrapper.find('[data-v2="positioning"]')
    expect(note.findAll('b').map((b) => b.text())).toEqual(['代练通侧', '不是'])
    expect(note.text().replace(/\s+/g, '')).toBe(
      '这里的模板是代练通侧的「订单要求模板」（上游OrderRequireTemp，发单到代练通时使用），不是本系统的「代练要求模板」——两者互不相干，别混用。',
    )
  })

  it('列表 4 列恰好集合（模板标题/模板内容/是否默认/操作）', async () => {
    const { wrapper } = await mountPage()
    const headers = wrapper.findAll('[data-v2="list"] thead th').map((th) => th.text())
    expect(headers).toEqual(['模板标题', '模板内容', '是否默认', '操作'])
  })

  it('默认行 = 绿实底签「默认」（cv2-st--settled）且不画「设为默认」；非默认 = 灰签「否」+ 有「设为默认」', async () => {
    const { wrapper } = await mountPage()
    const badgeA = wrapper.find('[data-v2-default-badge="11"]')
    expect(badgeA.text()).toBe('默认')
    expect(badgeA.classes()).toContain('cv2-st--settled')
    expect(rowOf(wrapper, 11).find('[data-v2-default]').exists()).toBe(false)

    const rowB = rowOf(wrapper, 12)
    expect(rowB.find('.cv2-st--unassigned').text()).toBe('否')
    expect(rowB.find('[data-v2-default="12"]').exists()).toBe(true)
    expect(rowB.find('[data-v2-delete="12"]').exists()).toBe(true)
    expect(rowB.find('[data-v2-edit="12"]').exists()).toBe(true)
    expect(rowB.find('[data-v2-cell="temp-1"]').attributes('title')).toBe('示例要求 B')
  })

  it('`id=null` 的行：操作列画「—」、无任何写按钮；空标题/内容格画「—」', async () => {
    const { wrapper } = await mountPage()
    const rowNull = rowOf(wrapper, 'row-2')
    expect(rowNull.exists()).toBe(true)
    expect(rowNull.find('.cv2-td-actions').text()).toBe('—')
    expect(rowNull.findAll('button')).toHaveLength(0)
    const rowEmpty = rowOf(wrapper, 13)
    expect(rowEmpty.findAll('td')[0]!.text()).toBe('—')
    expect(rowEmpty.findAll('td')[1]!.text()).toBe('—')
  })

  it('状态行三态：共 N 条模板（服务端总数 M）/ record_count=null 省略总数段 / 还没有模板 / 失败 + 重试', async () => {
    const first = await mountPage()
    expect(first.wrapper.find('[data-v2="status"]').text()).toBe('共 4 条模板（服务端总数 7）')

    listDailiantongOrderTemplates.mockResolvedValue({ items: LIST_PAYLOAD.items.map((r) => ({ ...r })), record_count: null })
    const second = await mountPage()
    expect(second.wrapper.find('[data-v2="status"]').text()).toBe('共 4 条模板')

    listDailiantongOrderTemplates.mockResolvedValue({ items: [], record_count: null })
    const third = await mountPage()
    expect(third.wrapper.find('[data-v2="status"]').text()).toBe('还没有模板')
    expect(third.wrapper.find('[data-v2="empty"]').text()).toBe('还没有模板')

    listDailiantongOrderTemplates.mockRejectedValue(new Error('合成错误：代练通账号未绑定'))
    const fourth = await mountPage()
    expect(fourth.wrapper.find('[data-v2="status"]').text()).toBe('加载失败：合成错误：代练通账号未绑定重试')
    expect(fourth.wrapper.find('[data-v2="status"]').classes()).toContain('cv2-status--error')

    listDailiantongOrderTemplates.mockClear()
    listDailiantongOrderTemplates.mockResolvedValue({ ...LIST_PAYLOAD, items: LIST_PAYLOAD.items.map((r) => ({ ...r })) })
    await fourth.wrapper.find('[data-v2="retry"]').trigger('click')
    await flushPromises()
    expect(listDailiantongOrderTemplates).toHaveBeenCalledTimes(1)
    expect(fourth.wrapper.find('[data-v2="status"]').text()).toContain('共 4 条模板')
  })
})

describe('V2DailiantongTemplates：新增与本地预检', () => {
  it('新增真交互：POST 载荷钉值（三项全发）→ 说明条「模板已创建」绿字、不弹 toast、表单复位、列表重取', async () => {
    const { wrapper } = await mountPage()
    await fillForm(wrapper, { title: '合成模板新', content: '示例要求新', isDefault: true })
    await wrapper.find('[data-v2="create"]').trigger('click')
    await flushPromises()
    expect(createDailiantongOrderTemplate).toHaveBeenCalledWith({
      title: '合成模板新',
      temp: '示例要求新',
      is_default: true,
    })
    const message = wrapper.find('[data-v2="action-message"]')
    expect(message.text()).toBe('模板已创建')
    expect(message.classes()).toContain('cv2-action-msg--ok')
    expect(wrapper.find('[data-v2="toast"]').exists()).toBe(false)
    expect(titleValue(wrapper)).toBe('')
    expect(listDailiantongOrderTemplates).toHaveBeenCalledTimes(2)
  })

  it('预检①：标题为空 ⇒ 零请求，toast + 说明条两处都出「请填写模板标题」', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="create"]').trigger('click')
    await flushPromises()
    expect(createDailiantongOrderTemplate).not.toHaveBeenCalled()
    expect(wrapper.find('[data-v2="toast"]').text()).toBe('请填写模板标题')
    expect(wrapper.find('[data-v2="action-message"]').text()).toBe('请填写模板标题')
    expect(wrapper.find('[data-v2="action-message"]').classes()).toContain('cv2-action-msg--error')
  })

  it('预检②：内容为空 ⇒ 零请求，「请填写模板内容」两处都出', async () => {
    const { wrapper } = await mountPage()
    await fillForm(wrapper, { title: '只有标题' })
    await wrapper.find('[data-v2="create"]').trigger('click')
    await flushPromises()
    expect(createDailiantongOrderTemplate).not.toHaveBeenCalled()
    expect(wrapper.find('[data-v2="action-message"]').text()).toBe('请填写模板内容')
  })

  it('输入框上限照后端序列化器：标题 maxlength 64 / 内容 textarea maxlength 1024（rows 3）', async () => {
    const { wrapper } = await mountPage()
    const title = wrapper.find('[data-v2-field="title"]').element as HTMLInputElement
    const content = wrapper.find('[data-v2-field="content"]').element as HTMLTextAreaElement
    expect(title.getAttribute('maxlength')).toBe('64')
    expect(content.getAttribute('maxlength')).toBe('1024')
  })
})

describe('V2DailiantongTemplates：编辑（回填顶部表单，非弹层）', () => {
  it('点「编辑」⇒ 表单回填、新增模板换成保存修改+取消编辑；保存修改 PATCH 三项全发 →「模板已更新」+ 复位', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, 12).find('[data-v2-edit="12"]').trigger('click')
    await flushPromises()
    expect(titleValue(wrapper)).toBe('合成模板乙')
    expect(
      (wrapper.find('[data-v2-field="content"]').element as HTMLTextAreaElement).value,
    ).toBe('示例要求 B')
    expect(wrapper.find('[data-v2="create"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="save"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2="cancel"]').exists()).toBe(true)

    await fillForm(wrapper, { title: '合成模板乙改' })
    await wrapper.find('[data-v2="save"]').trigger('click')
    await flushPromises()
    expect(updateDailiantongOrderTemplate).toHaveBeenCalledWith(12, {
      title: '合成模板乙改',
      temp: '示例要求 B',
      is_default: false,
    })
    expect(wrapper.find('[data-v2="action-message"]').text()).toBe('模板已更新')
    expect(wrapper.find('[data-v2="create"]').exists()).toBe(true)
    expect(titleValue(wrapper)).toBe('')
  })

  it('取消编辑 ⇒ 回新建态、表单清空、说明条清掉', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, 12).find('[data-v2-edit="12"]').trigger('click')
    await fillForm(wrapper, { title: '改了一半' })
    await wrapper.find('[data-v2="cancel"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="create"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2="save"]').exists()).toBe(false)
    expect(titleValue(wrapper)).toBe('')
    expect(wrapper.find('[data-v2="action-message"]').text()).toBe('')
  })
})

describe('V2DailiantongTemplates：删除 / 设默认（必须先确认）', () => {
  it('删除：确认弹层标题「删除模板」+ 句子逐字；取消零请求；确定发 DELETE →「模板已删除」', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, 12).find('[data-v2-delete="12"]').trigger('click')
    await flushPromises()
    const dlg = wrapper.find('[data-v2="dlg-confirm"]')
    expect(dlg.exists()).toBe(true)
    expect(dlg.find('.el-dialog__title').text()).toBe('删除模板')
    expect(dlg.find('[data-v2="confirm-text"]').text()).toBe(
      '确定删除模板「合成模板乙」吗？删除后不可恢复，请确认。',
    )

    await dlg.find('[data-v2="confirm-cancel"]').trigger('click')
    await flushPromises()
    expect(deleteDailiantongOrderTemplate).not.toHaveBeenCalled()
    // 关闭后弹层整体 v-show 隐藏（EP 离场过渡在 jsdom 不销毁内容，只看可见性）。
    expect(wrapper.find('[data-v2="confirm-text"]').isVisible()).toBe(false)

    await rowOf(wrapper, 12).find('[data-v2-delete="12"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-v2="confirm-ok"]').trigger('click')
    await flushPromises()
    expect(deleteDailiantongOrderTemplate).toHaveBeenCalledWith(12)
    expect(wrapper.find('[data-v2="action-message"]').text()).toBe('模板已删除')
    expect(listDailiantongOrderTemplates).toHaveBeenCalledTimes(2)
  })

  it('删除空标题行：确认句回落「模板 13」；删除正在编辑的那条 ⇒ 表单复位回新建态', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, 13).find('[data-v2-edit="13"]').trigger('click')
    expect(wrapper.find('[data-v2="save"]').exists()).toBe(true)
    await rowOf(wrapper, 13).find('[data-v2-delete="13"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="confirm-text"]').text()).toBe(
      '确定删除模板「模板 13」吗？删除后不可恢复，请确认。',
    )
    await wrapper.find('[data-v2="confirm-ok"]').trigger('click')
    await flushPromises()
    expect(deleteDailiantongOrderTemplate).toHaveBeenCalledWith(13)
    expect(wrapper.find('[data-v2="create"]').exists()).toBe(true)
    expect(titleValue(wrapper)).toBe('')
  })

  it('设默认：确认弹层标题「设为默认模板」+ 句子逐字；确定 POST default →「已设为默认模板」', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, 12).find('[data-v2-default="12"]').trigger('click')
    await flushPromises()
    const dlg = wrapper.find('[data-v2="dlg-confirm"]')
    expect(dlg.find('.el-dialog__title').text()).toBe('设为默认模板')
    expect(dlg.find('[data-v2="confirm-text"]').text()).toBe(
      '确定把模板「合成模板乙」设为默认吗？代练通发单会默认使用它。',
    )
    await dlg.find('[data-v2="confirm-ok"]').trigger('click')
    await flushPromises()
    expect(setDefaultDailiantongOrderTemplate).toHaveBeenCalledWith(12)
    expect(wrapper.find('[data-v2="action-message"]').text()).toBe('已设为默认模板')
  })
})

describe('V2DailiantongTemplates：失败路径与 busy 在途闸', () => {
  it('失败 ⇒ toast（6000 错误档）+ 说明条「操作失败：<后端原文>」两处都出；表单不复位', async () => {
    createDailiantongOrderTemplate.mockRejectedValue(new Error('模板数量已达上限（合成）'))
    const { wrapper } = await mountPage()
    await fillForm(wrapper, { title: '合成模板新', content: '示例要求新' })
    await wrapper.find('[data-v2="create"]').trigger('click')
    await flushPromises()
    const toastEl = wrapper.find('[data-v2="toast"]')
    expect(toastEl.text()).toBe('模板数量已达上限（合成）')
    expect(toastEl.classes()).toContain('cv2-toast--error')
    expect(wrapper.find('[data-v2="action-message"]').text()).toBe('操作失败：模板数量已达上限（合成）')
    expect(wrapper.find('[data-v2="action-message"]').classes()).toContain('cv2-action-msg--error')
    expect(titleValue(wrapper)).toBe('合成模板新')
  })

  it('提交中禁用全部按钮（表单钮/刷新/行内钮）+ 连点只发一次；完成后恢复', async () => {
    let resolveCreate!: (value: unknown) => void
    createDailiantongOrderTemplate.mockImplementation(
      () => new Promise((resolve) => { resolveCreate = resolve }),
    )
    const { wrapper } = await mountPage()
    await fillForm(wrapper, { title: '合成模板新', content: '示例要求新' })
    await wrapper.find('[data-v2="create"]').trigger('click')
    await flushPromises()
    expect(createDailiantongOrderTemplate).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-v2="create"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-v2="refresh"]').attributes('disabled')).toBeDefined()
    expect(rowOf(wrapper, 12).find('[data-v2-edit="12"]').attributes('disabled')).toBeDefined()
    // 在途闸：再点也不发第二条。
    await wrapper.find('[data-v2="create"]').trigger('click')
    expect(createDailiantongOrderTemplate).toHaveBeenCalledTimes(1)

    resolveCreate({ ...TPL_B })
    await flushPromises()
    expect(wrapper.find('[data-v2="create"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-v2="action-message"]').text()).toBe('模板已创建')
  })
})
