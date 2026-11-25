// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 「交易平台自动回复」v2（V2PlatformAutoReply）的保真网：mock API → 钉值渲染（8 列恰好集合 /
 * 全部游戏显示 / 图片链接）、弹层（时机 15/20 与 12 个非代练通平台置灰；未选时机提交仍拦）、
 * 凭据与图片纪律（本地先拦 2 MiB；image_key 只在本次新上传时随载荷发；新增不发 is_enabled）、
 * 批量修改逐行**顺序** PATCH + 中断提示钉值、删除确认文案逐字。文案出处 = island 复刻页
 * `system-platformautoreply.ts` 与证据件 HTML（组件常量表有行号）；全部夹具都是**合成值**。
 */
const fetchAllGames = vi.fn()
const fetchPlatformAutoReplyRules = vi.fn()
const createPlatformAutoReplyRule = vi.fn()
const updatePlatformAutoReplyRule = vi.fn()
const deletePlatformAutoReplyRule = vi.fn()
const uploadPlatformAutoReplyImage = vi.fn()

vi.mock('@/api/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/config')>()),
  fetchAllGames: (...args: unknown[]) => fetchAllGames(...args),
}))
vi.mock('@/api/platformAutoReplies', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/platformAutoReplies')>()),
  fetchPlatformAutoReplyRules: (...args: unknown[]) => fetchPlatformAutoReplyRules(...args),
  createPlatformAutoReplyRule: (...args: unknown[]) => createPlatformAutoReplyRule(...args),
  updatePlatformAutoReplyRule: (...args: unknown[]) => updatePlatformAutoReplyRule(...args),
  deletePlatformAutoReplyRule: (...args: unknown[]) => deletePlatformAutoReplyRule(...args),
  uploadPlatformAutoReplyImage: (...args: unknown[]) => uploadPlatformAutoReplyImage(...args),
}))

import { ElSelect } from 'element-plus'

import V2PlatformAutoReply from '../V2PlatformAutoReply.vue'
import { useSessionStore } from '@/stores/session'
import type { PlatformAutoReplyRuleRow } from '@/api/platformAutoReplies'

/* ── 合成夹具 ─────────────────────────────────────────────────────────────────── */

const RULE_A: PlatformAutoReplyRuleRow = {
  id: 21,
  platform: 10,
  platform_label: '代练通',
  game_id: null,
  game: null,
  game_name: '',
  trigger: 0,
  trigger_name: '接单后',
  content: '合成回复一',
  has_image: false,
  image_url: '',
  is_enabled: true,
  created_at: '2026-10-01T08:00:00+08:00',
  updated_at: '2026-10-01T08:00:00+08:00',
}

const RULE_B: PlatformAutoReplyRuleRow = {
  ...RULE_A,
  id: 22,
  game_id: 2,
  game: 2,
  game_name: '示例游戏二',
  trigger: 10,
  trigger_name: '申请验收后',
  content: '合成回复二',
  has_image: true,
  image_url: 'https://cos.example.com/syn-rule-22.png?sign=syn&exp=1',
  is_enabled: false,
}

const GAMES = [
  { id: 2, name: '示例游戏二', sort_order: 1, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
  { id: 3, name: '示例游戏三', sort_order: 2, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
]

function makeRouter(): Router {
  const Blank = { template: '<div />' }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', name: 'v2', component: Blank, children: [{ path: '', name: 'v2-home', component: Blank }] },
      { path: '/legacy-auto-reply', name: 'system-platform-auto-reply', component: Blank },
    ],
  })
}

function makeSession(permissions: string[]): void {
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
    menu_permissions: permissions,
    tenant_name: '示例工作室甲',
  }
  session.status = 'ready'
}

async function mountPage(permissions: string[] = ['system.platform_auto_reply']) {
  const pinia = createPinia()
  setActivePinia(pinia)
  makeSession(permissions)
  const router = makeRouter()
  await router.push('/v2')
  await router.isReady()
  const wrapper = mount(V2PlatformAutoReply, { global: { plugins: [router, pinia] } })
  await flushPromises()
  return { wrapper, router }
}

function toastOf(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']): string {
  const el = wrapper.find('[data-v2="toast"]')
  return el.exists() ? el.text() : ''
}

function rowOf(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], id: number) {
  return wrapper.find(`[data-v2-row="${id}"]`)
}

function selectByAnchor(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], anchor: string) {
  const found = wrapper
    .findAllComponents(ElSelect)
    .find((c) => c.attributes('data-v2-field') === anchor)
  expect(found, `应存在 data-v2-field="${anchor}" 的 el-select`).toBeDefined()
  return found!
}

/** 往隐藏文件框塞一个 File 并触发 change。 */
async function pickFile(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], file: File) {
  const input = wrapper.find('[data-v2="reply-image-input"]')
  Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
  await input.trigger('change')
}

beforeEach(() => {
  vi.clearAllMocks()
  fetchPlatformAutoReplyRules.mockResolvedValue([RULE_A, RULE_B])
  fetchAllGames.mockResolvedValue(GAMES)
})

describe('V2PlatformAutoReply：权限门（fail-closed）', () => {
  it('无 system.platform_auto_reply ⇒ 「无权限查看本页」空态，不发任何请求', async () => {
    const { wrapper } = await mountPage([])
    expect(wrapper.find('[data-v2="no-permission"]').text()).toBe('无权限查看本页')
    expect(fetchPlatformAutoReplyRules).not.toHaveBeenCalled()
    expect(fetchAllGames).not.toHaveBeenCalled()
  })
})

describe('V2PlatformAutoReply：钉值渲染', () => {
  it('8 列表头恰好集合；两行渲染；全部游戏 / 时机 / 启用平台 / 计数句；引文块逐字', async () => {
    const { wrapper } = await mountPage()
    const headers = wrapper.findAll('.cv2-table thead th').map((t) => t.text().trim())
    expect(headers).toEqual(['', '限定游戏', '回复时机', '回复内容', '图片', '启用平台', '启用状态', '操作'])
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(2)
    expect(rowOf(wrapper, RULE_A.id).find('[data-v2-cell="game-21"]').text()).toBe('全部游戏')
    expect(rowOf(wrapper, RULE_B.id).find('[data-v2-cell="game-22"]').text()).toBe('示例游戏二')
    expect(rowOf(wrapper, RULE_B.id).find('[data-v2-cell="trigger-22"]').text()).toBe('申请验收后')
    expect(rowOf(wrapper, RULE_B.id).find('[data-v2-cell="platform-22"]').text()).toBe('代练通')
    expect(wrapper.find('[data-v2="count"]').text()).toBe('共 2 条规则')
    const quote = wrapper.find('[data-v2="page-quote"]').text()
    expect(quote).toContain('功能介绍：')
    expect(quote).toContain('您可以按需在此配置自动回复交易平台的打手留言消息，当交易平台有对应动作时，将会自动回复消息给打手；')
    expect(quote).toContain('前提条件：')
    expect(quote).toContain('要求您已在本系统对接过第三方交易平台')
  })

  it('游戏字典加载失败 ⇒ toast「游戏列表加载失败：<原文>」，列表照常渲染', async () => {
    fetchAllGames.mockRejectedValue(new Error('合成失败：字典被拒'))
    const { wrapper } = await mountPage()
    expect(toastOf(wrapper)).toBe('游戏列表加载失败：合成失败：字典被拒')
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(2)
  })
})

describe('V2PlatformAutoReply：新增/修改弹层', () => {
  it('新增标题逐字；游戏下拉第一项「全部游戏」+ 我方游戏；15/20 时机置灰且 title=尚未开放，0/10 可选', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="add"]').trigger('click')
    const dlg = wrapper.find('[data-v2="dlg-form"]')
    expect(dlg.text()).toContain('新增自动回复')
    expect(dlg.text()).toContain('根据实际需要，请在下面配置')
    const gameSelect = selectByAnchor(wrapper, 'game')
    expect(gameSelect.props('modelValue')).toBe(null)
    const options = gameSelect.findAllComponents({ name: 'ElOption' })
    expect(options[0]!.props('label')).toBe('全部游戏')
    expect(options.map((o) => o.props('label'))).toContain('示例游戏三')
    const enabled = (key: number): boolean =>
      wrapper.find(`[data-v2-trigger-opt="${key}"] input`).attributes('disabled') === undefined
    expect(enabled(0)).toBe(true)
    expect(enabled(10)).toBe(true)
    expect(enabled(15)).toBe(false)
    expect(enabled(20)).toBe(false)
    expect(wrapper.find('[data-v2-trigger-opt="15"]').attributes('title')).toBe('尚未开放')
    expect(wrapper.find('[data-v2-trigger-opt="20"]').attributes('title')).toBe('尚未开放')
  })

  it('平台 13 checkbox：只有「代练通」可勾且恒选中，其余 12 颗置灰', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="add"]').trigger('click')
    const boxes = wrapper.findAll('[data-v2-platform-check]')
    expect(boxes).toHaveLength(13)
    // 收集后统一断言（避免条件分支内 expect）：勾选与可勾的都恰好只有「10 代练通」。
    const checkedIds = boxes
      .filter((b) => (b.element as HTMLInputElement).checked)
      .map((b) => b.attributes('data-v2-platform-check'))
    const enabledIds = boxes
      .filter((b) => !(b.element as HTMLInputElement).disabled)
      .map((b) => b.attributes('data-v2-platform-check'))
    expect(checkedIds).toEqual(['10'])
    expect(enabledIds).toEqual(['10'])
  })

  it('校验逐字：未选时机 ⇒ 请选择回复时机；空内容 ⇒ 回复内容不能为空；通过后 POST（新增**不发 is_enabled / image_key**）', async () => {
    createPlatformAutoReplyRule.mockResolvedValue({ ...RULE_A, id: 30 })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="add"]').trigger('click')
    await wrapper.find('[data-v2="form-save"]').trigger('click')
    expect(toastOf(wrapper)).toBe('请选择回复时机')
    expect(createPlatformAutoReplyRule).not.toHaveBeenCalled()
    await wrapper.find('[data-v2-trigger-opt="0"] input').setValue(true)
    await wrapper.find('[data-v2="form-save"]').trigger('click')
    expect(toastOf(wrapper)).toBe('回复内容不能为空')
    expect(createPlatformAutoReplyRule).not.toHaveBeenCalled()
    await wrapper.find('[data-v2-field="content"]').setValue('  合成内容甲  ')
    await wrapper.find('[data-v2="form-save"]').trigger('click')
    await flushPromises()
    expect(createPlatformAutoReplyRule).toHaveBeenCalledTimes(1)
    const sent = createPlatformAutoReplyRule.mock.calls[0]?.[0] as Record<string, unknown>
    expect(sent).toEqual({ platform: 10, game: null, trigger: 0, content: '合成内容甲' })
    expect(sent).not.toHaveProperty('is_enabled')
    expect(sent).not.toHaveProperty('image_key')
    expect(toastOf(wrapper)).toBe('已保存')
  })

  it('编辑回填（游戏/时机/内容/图片）⇒ PATCH 载荷钉值；没换图不发 image_key；textarea placeholder 逐字', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, RULE_B.id).find('[data-v2-edit="22"]').trigger('click')
    const dlg = wrapper.find('[data-v2="dlg-form"]')
    expect(dlg.text()).toContain('修改自动回复')
    expect(selectByAnchor(wrapper, 'game').props('modelValue')).toBe(2)
    expect((wrapper.find('[data-v2-trigger-opt="10"] input').element as HTMLInputElement).checked).toBe(true)
    expect((wrapper.find('[data-v2-field="content"]').element as HTMLTextAreaElement).value).toBe('合成回复二')
    expect((wrapper.find('[data-v2-field="content"]').element as HTMLTextAreaElement).placeholder).toBe(
      '请在这里输入要自动回复的内容，不要有任何特殊代码',
    )
    updatePlatformAutoReplyRule.mockResolvedValue(RULE_B)
    await wrapper.find('[data-v2-field="content"]').setValue('合成回复二改')
    await wrapper.find('[data-v2="form-save"]').trigger('click')
    await flushPromises()
    const sent = updatePlatformAutoReplyRule.mock.calls[0]?.[1] as Record<string, unknown>
    expect(sent).toEqual({ platform: 10, game: 2, trigger: 10, content: '合成回复二改' })
    expect(sent).not.toHaveProperty('image_key')
  })

  it('编辑历史 15/20 时机行 ⇒ 保存明确拒绝（零请求），文案逐字照旧页 validatePanelValues（置灰只挡新选，挡不住回填值）', async () => {
    const RULE_OLD_TRIGGER: PlatformAutoReplyRuleRow = {
      ...RULE_A,
      id: 23,
      trigger: 15,
      trigger_name: '提交异常后',
      content: '合成历史规则',
    }
    fetchPlatformAutoReplyRules.mockResolvedValue([RULE_OLD_TRIGGER])
    const { wrapper } = await mountPage()
    await rowOf(wrapper, 23).find('[data-v2-edit="23"]').trigger('click')
    await wrapper.find('[data-v2="form-save"]').trigger('click')
    await flushPromises()
    expect(updatePlatformAutoReplyRule).not.toHaveBeenCalled()
    expect(createPlatformAutoReplyRule).not.toHaveBeenCalled()
    expect(toastOf(wrapper)).toBe('「提交异常后」尚未开放：本版只做「接单后」与「申请验收后」两种时机')
  })

  it('上传：本地先拦 2 MiB（零请求）；成功 ⇒ 缩略图 + toast，保存时 image_key 随载荷发；失败 ⇒ 上传失败：<原文>', async () => {
    uploadPlatformAutoReplyImage.mockResolvedValue({ image_key: 'syn-image-key-1', url: 'https://cos.example.com/syn-new.png?sign=x', expires_in: 300 })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="add"]').trigger('click')
    expect(wrapper.find('[data-v2="reply-image-input"]').attributes('accept')).toBe('image/jpeg,image/png')
    // 超限：2 MiB + 1 字节 ⇒ 本地拦，不碰端点。
    const big = new File(['syn'], 'big.png', { type: 'image/png' })
    Object.defineProperty(big, 'size', { value: 2 * 1024 * 1024 + 1 })
    await pickFile(wrapper, big)
    expect(toastOf(wrapper)).toBe('图片超过 2 MB，请压缩后再上传')
    expect(uploadPlatformAutoReplyImage).not.toHaveBeenCalled()
    // 成功：缩略图出现 + toast 逐字；上传中按钮禁用文案「上传中…」由 uploading 驱动（这里只断终态）。
    const ok = new File(['syn-image-bytes'], 'ok.png', { type: 'image/png' })
    await pickFile(wrapper, ok)
    await flushPromises()
    expect(uploadPlatformAutoReplyImage).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-v2="pic-preview"]').attributes('src')).toBe('https://cos.example.com/syn-new.png?sign=x')
    expect(toastOf(wrapper)).toBe('图片已上传，保存后生效')
    // image_key 只在本次新上传时随载荷发。
    await wrapper.find('[data-v2-trigger-opt="0"] input').setValue(true)
    await wrapper.find('[data-v2-field="content"]').setValue('带图规则')
    createPlatformAutoReplyRule.mockResolvedValue({ ...RULE_A, id: 31 })
    await wrapper.find('[data-v2="form-save"]').trigger('click')
    await flushPromises()
    const sent = createPlatformAutoReplyRule.mock.calls[0]?.[0] as Record<string, unknown>
    expect(sent['image_key']).toBe('syn-image-key-1')
    // 失败：不落 image_key（上一张的 key 不复用——openForm 复位）。
    await wrapper.find('[data-v2="add"]').trigger('click')
    expect(wrapper.find('[data-v2="pic-preview"]').exists()).toBe(false)
    uploadPlatformAutoReplyImage.mockRejectedValueOnce(new Error('格式不支持'))
    await pickFile(wrapper, new File(['x'], 'bad.gif', { type: 'image/gif' }))
    await flushPromises()
    expect(toastOf(wrapper)).toBe('上传失败：格式不支持')
  })

  it('保存失败 ⇒ 保存失败：<后端原文>', async () => {
    createPlatformAutoReplyRule.mockRejectedValue(new Error('合成失败：内容被拒'))
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="add"]').trigger('click')
    await wrapper.find('[data-v2-trigger-opt="0"] input').setValue(true)
    await wrapper.find('[data-v2-field="content"]').setValue('合成内容乙')
    await wrapper.find('[data-v2="form-save"]').trigger('click')
    await flushPromises()
    expect(toastOf(wrapper)).toBe('保存失败：合成失败：内容被拒')
  })
})

describe('V2PlatformAutoReply：行内开关 / 查看图片 / 删除', () => {
  it('启用开关 PATCH {is_enabled} 单字段 + 整表重取；失败 ⇒ toast 后端原文 + 重取', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, RULE_B.id).find('[data-v2-switch="is_enabled-22"] input').setValue(true)
    await flushPromises()
    expect(updatePlatformAutoReplyRule).toHaveBeenCalledWith(22, { is_enabled: true })
    expect(fetchPlatformAutoReplyRules).toHaveBeenCalledTimes(2)
    updatePlatformAutoReplyRule.mockRejectedValueOnce(new Error('合成失败：开关被拒'))
    await rowOf(wrapper, RULE_A.id).find('[data-v2-switch="is_enabled-21"] input').setValue(false)
    await flushPromises()
    expect(toastOf(wrapper)).toBe('合成失败：开关被拒')
    expect(fetchPlatformAutoReplyRules).toHaveBeenCalledTimes(3)
  })

  it('查看图片：无图 ⇒ toast「未上传图片」零弹层；有图 ⇒ lg 弹层装现签 URL（即点即取行数据）', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, RULE_A.id).find('[data-v2-view-image="21"]').trigger('click')
    expect(toastOf(wrapper)).toBe('未上传图片')
    expect(wrapper.find('[data-v2="dlg-image"]').exists()).toBe(false)
    await rowOf(wrapper, RULE_B.id).find('[data-v2-view-image="22"]').trigger('click')
    const dlg = wrapper.find('[data-v2="dlg-image"]')
    expect(dlg.exists()).toBe(true)
    expect(dlg.classes()).toContain('cv2-dialog--lg')
    expect(dlg.find('[data-v2="image-full"]').attributes('src')).toBe(RULE_B.image_url)
  })

  it('删除确认语与按钮逐字；「点错了」零请求；「确认删除」⇒ DELETE + 已删除 + 重取', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, RULE_A.id).find('[data-v2-delete="21"]').trigger('click')
    expect(wrapper.find('[data-v2="confirm-text"]').text()).toBe('是否确认删除这条自动回复？')
    expect(wrapper.find('[data-v2="confirm-cancel"]').text()).toBe('点错了')
    expect(wrapper.find('[data-v2="confirm-delete"]').text()).toBe('确认删除')
    await wrapper.find('[data-v2="confirm-cancel"]').trigger('click')
    expect(deletePlatformAutoReplyRule).not.toHaveBeenCalled()
    deletePlatformAutoReplyRule.mockResolvedValue(null)
    await rowOf(wrapper, RULE_A.id).find('[data-v2-delete="21"]').trigger('click')
    await wrapper.find('[data-v2="confirm-delete"]').trigger('click')
    await flushPromises()
    expect(deletePlatformAutoReplyRule).toHaveBeenCalledWith(21)
    expect(toastOf(wrapper)).toBe('已删除')
    expect(fetchPlatformAutoReplyRules).toHaveBeenCalledTimes(2)
  })
})

describe('V2PlatformAutoReply：批量修改（逐行顺序 PATCH）', () => {
  it('未勾行点批量 ⇒ toast「请勾选要操作的数据行」零请求；勾两行 ⇒ 弹层标题/引文逐字；空内容拦', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="batch"]').trigger('click')
    expect(toastOf(wrapper)).toBe('请勾选要操作的数据行')
    expect(updatePlatformAutoReplyRule).not.toHaveBeenCalled()
    await rowOf(wrapper, RULE_A.id).find('[data-v2-row-check="21"]').setValue(true)
    await rowOf(wrapper, RULE_B.id).find('[data-v2-row-check="22"]').setValue(true)
    await wrapper.find('[data-v2="batch"]').trigger('click')
    const dlg = wrapper.find('[data-v2="dlg-batch"]')
    expect(dlg.text()).toContain('批量修改自动回复内容')
    expect(dlg.text()).toContain('这个功能是作用于自动回复内容一致的游戏设置，当您在填写完回复内容保存后，所有已勾选的游戏消息设置，都将统一变更为刚保存的自动回复内容')
    await wrapper.find('[data-v2="batch-save"]').trigger('click')
    expect(toastOf(wrapper)).toBe('回复内容不能为空')
    expect(updatePlatformAutoReplyRule).not.toHaveBeenCalled()
  })

  it('两条全部成功 ⇒ 逐行顺序 PATCH {content}（调用序 = 勾选序）+ 已保存 + 重取', async () => {
    updatePlatformAutoReplyRule.mockResolvedValue(RULE_A)
    const { wrapper } = await mountPage()
    await rowOf(wrapper, RULE_A.id).find('[data-v2-row-check="21"]').setValue(true)
    await rowOf(wrapper, RULE_B.id).find('[data-v2-row-check="22"]').setValue(true)
    await wrapper.find('[data-v2="batch"]').trigger('click')
    await wrapper.find('[data-v2-field="batch-content"]').setValue('统一内容')
    await wrapper.find('[data-v2="batch-save"]').trigger('click')
    await flushPromises()
    expect(updatePlatformAutoReplyRule).toHaveBeenCalledTimes(2)
    expect(updatePlatformAutoReplyRule.mock.calls[0]).toEqual([21, { content: '统一内容' }])
    expect(updatePlatformAutoReplyRule.mock.calls[1]).toEqual([22, { content: '统一内容' }])
    expect(toastOf(wrapper)).toBe('已保存')
    expect(fetchPlatformAutoReplyRules).toHaveBeenCalledTimes(2)
  })

  it('第 2 条失败 ⇒ 中断提示钉值「已更新 1 / 2 条，第 2 条失败：<原文>」+ 以服务端为准重取', async () => {
    updatePlatformAutoReplyRule.mockResolvedValueOnce(RULE_A)
    updatePlatformAutoReplyRule.mockRejectedValueOnce(new Error('合成失败：第 2 条被拒'))
    const { wrapper } = await mountPage()
    await rowOf(wrapper, RULE_A.id).find('[data-v2-row-check="21"]').setValue(true)
    await rowOf(wrapper, RULE_B.id).find('[data-v2-row-check="22"]').setValue(true)
    await wrapper.find('[data-v2="batch"]').trigger('click')
    await wrapper.find('[data-v2-field="batch-content"]').setValue('统一内容')
    await wrapper.find('[data-v2="batch-save"]').trigger('click')
    await flushPromises()
    expect(updatePlatformAutoReplyRule).toHaveBeenCalledTimes(2)
    expect(toastOf(wrapper)).toBe('已更新 1 / 2 条，第 2 条失败：合成失败：第 2 条被拒')
    expect(wrapper.find('[data-v2="toast"]').classes()).toContain('cv2-toast--error')
    expect(fetchPlatformAutoReplyRules).toHaveBeenCalledTimes(2)
  })
})

describe('V2PlatformAutoReply：工具条', () => {
  it('刷新列表 ⇒ 重取 + 已刷新；列表失败态带 重试 与 后端原文', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="refresh"]').trigger('click')
    await flushPromises()
    expect(fetchPlatformAutoReplyRules).toHaveBeenCalledTimes(2)
    expect(toastOf(wrapper)).toBe('已刷新')
    fetchPlatformAutoReplyRules.mockRejectedValue(new Error('合成失败：列表被拒'))
    await wrapper.find('[data-v2="refresh"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="empty"]').text()).toContain('加载失败：合成失败：列表被拒')
    expect(wrapper.find('[data-v2="retry"]').text()).toBe('重试')
  })
})
