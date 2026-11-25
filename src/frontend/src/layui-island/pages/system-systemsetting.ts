/**
 * 【系统设置】页的 **layui 复刻页**（对标物 `/System/systemsetting`，证据件 + **SK32 一条真开关**）。
 *
 * ⭐ **系统开关线（D-0223，2026-10-10）**：本页从 `artifactPage`（纯证据件渲染）改为
 * **自取证据件 + 自有委托**（先例：`system-autodiscernset.ts` / `system-sourelist.ts`），
 * 但**只接 SK32 一条**（「录单时禁止录入相同的来源单号」）；页内其余 60+ 条设置项
 * 照对标渲染、点了提示「尚未开放」（D-0025「无后端的部分照对标渲染 + 点了提示」）。
 *
 * 形状：**页签式设置页**（`layui-tab` 6 个页签 + 大量 `fieldset`/表单 + 一张藏在第 5 个
 * 页签「老板查进度设置」里、默认不可见的表格——表格结构与列定义**逐字保留**）。
 *
 * 证据（值一律从对标取）：
 *  - 标记与页内 `<style>` → `public/replica/system-systemsetting.html`（96 KB，零转义）
 *  - SK32 的两个 radio → 证据件 L247-248（`name="SK32"`，值 `N`=「不要求」/`Y`=「是的不允许」）
 *  - 保存按钮 → 证据件里 **5 颗** `lay-filter="save2btn"`（L831/926/1004/1131/1261，
 *    对标全部共用同一个 `form.on('submit(save2btn)')`）
 *  - 列定义 → `.tools/recon/suqing1/assets/JS2019/System/systemsetting.js` **L164-194**
 *  - 对标保存交互 → 同 JS **L83-91**：把整个表单 `data.field` JSON 打包 POST
 *    `/SystemAjax/SystemOrderSetting`（L113-115），成功 `layer.msg` + `location.reload(true)`
 *
 * ## 为什么不再走 `artifactPage`（同 autodiscernset 的理由）
 *
 * `artifactPage` 装的捕获阶段「尚未开放」总闸会把一切可点元素先截走 ⇒ SK32 的 radio
 * 与保存按钮点了没反应。本页**自建**同一款总闸（照抄 `artifactPage.ts` 的收口逻辑与
 * `clickLabel`），但**放过两条路**：SK32 的 radio 组 + `save2btn` 的 submit。
 * 漏了这步，其余 60+ 条设置会变成「点了没反应」的死控件（总闸必须补，E-0061 同族）。
 *
 * ## 三条口径（每条都写进了页测试）
 *
 * | # | 口径 | 判据 |
 * |---|---|---|
 * | 1 | 载入后 SK32 勾选态来自**我们后端**（证据件里 `checked` 是对标租户 suqing 的数据，⛔ 不能当我们的默认） | `form.val('sform', {SK32: 后端值})` 覆盖 |
 * | 2 | 保存**只送 SK32 一条**（对标是全量表单 JSON 提交——「已知会不同」，登记在 FRONTEND-REPLICA-STANDARD §7） | `PATCH /api/business/system-switches/<key>/` body `{value}` |
 * | 3 | 失败原样弹后端 `msg`、勾选态**回滚**到保存前的值（对标失败只 `layer.msg`；回滚是我们的补充——不回滚会把「没保存上」伪装成「已保存」） | 先记旧值再发请求 |
 *
 * ## 与对标并排比对的差异（原因与证据等级）
 *
 * | # | 差异 | 原因 |
 * |---|---|---|
 * | 1 | 保存成功后**重取一次**开关值刷新勾选态，不是 `location.reload` | island 页跑在 iframe 里，整页 reload 会把 island 外壳一起刷掉；重取与 reload 语义等效（读到刚保存的值） |
 * | 2 | SK32 的勾选态来自我们后端 | 对标来自它自己租户的数据（同一条「值流去哪个端点」的判据，E-0068） |
 * | 3 | 其余 SK 条目的标记**一个字不改** | 仍是证据件原文（页内除 SK32 相关委托外零标记改动） |
 *
 * ## 列宽：两列以上是**运行期表达式**，此处按**桌面分支**人工定值
 *
 * `systemsetting.js` L166-190 的 5 个 `width` 全是 `isPc ? '<百分比>' : <像素>`
 * （同 JS L9-13：`isPc` 默认真）⇒ 桌面分支取值 `30% / 20% / 12% / 12% / 12%`。
 * `ArtifactCol.width` 的类型是 `number`，百分比只能显式收窄（`pct()`）。
 * 证据等级：**读码 + 人工定值**（该表默认隐藏，两侧渲染宽都是 0，探针量不到）。
 */
import {
  fetchSystemSwitches,
  updateSystemSwitch,
  type SystemSwitchRow,
} from '@/api/systemSwitches'
import { loadConfigArtifact } from '@/layui-island/config-replica'
import { clickLabel, type ArtifactCol } from '@/layui/artifactPage'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  islandTabTarget,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** 页 `<title>`（对标服务端渲染值，证据件源 `System__systemsetting.html` L5，逐字）。 */
export const TITLE = '系统设置'
/** 证据件名（= island key = `pages/<key>.ts` 的文件名）。 */
const ARTIFACT_KEY = 'system-systemsetting'
/** 对标 `table.render` 的 `elem` / `reload` 首参（`systemsetting.js` L165；id 不带 `#`）。 */
export const TABLE_ELEM = '#tableList'
export const TABLE_ID = 'tableList'
/** 整页大表单的 `lay-filter`（证据件 L81）——`form.val` 按它作用域预填。 */
export const FORM_FILTER = 'sform'
/** 我们接真的那一条开关：radio 的 `name`（= 对标 SK 编号）与我们后端的 key。 */
export const SK32_RADIO_NAME = 'SK32'
export const SK32_SWITCH_KEY = 'forbid_duplicate_source_order_no'
/** 保存按钮的 `lay-filter`（证据件里 5 颗共用它）。 */
export const SAVE_FILTER = 'save2btn'

/**
 * 百分数列宽的显式标注：对标写的是运行期表达式 `isPc ? '30%' : 300`，
 * 这里取**桌面分支**的字面量。`ArtifactCol.width` 只声明了 `number`，故收窄一次。
 */
const pct = (value: string): number => value as unknown as number

export const SYSTEMSETTING_COLS: ArtifactCol[] = [
  // 逐条照抄 `systemsetting.js` L165-193（`sort: false` / `unresize: false` 是 layui 默认值、
  // `fixed: ''` = 不固定，均不写；`edit: true` 与 `templet` 在空表下不渲染，故不搬）。
  { field: 'SeachQuestion_Title', title: '标题', width: pct('30%'), align: 'left' },
  { field: 'Hot', title: '热度(越大排越前)', width: pct('20%'), align: 'center' },
  { field: 'Status', title: '是否需要显示出来', width: pct('12%'), align: 'center' },
  { field: 'createTimeStr', title: '发布时间', width: pct('12%'), align: 'center' },
  { title: '操作', width: pct('12%'), unresize: true },
]

/** 取「SK32 当前选中的那个值」（`"Y"` / `"N"`；一个都没选中 ⇒ `null`）。 */
export function checkedSk32Value(root: HTMLElement | Document = document): 'Y' | 'N' | null {
  const input = root.querySelector<HTMLInputElement>(
    `input[name="${SK32_RADIO_NAME}"]:checked`,
  )
  const value = input?.value
  return value === 'Y' || value === 'N' ? value : null
}

/** 判断一次点击是否落在 **SK32 的 radio 组**里（本页接真的唯一一块表单区）。 */
export function withinSk32Block(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  if (!el || typeof el.closest !== 'function') return false
  for (const input of document.querySelectorAll<HTMLInputElement>(
    `input[name="${SK32_RADIO_NAME}"]`,
  )) {
    // 原生 radio 与 layui 渲染出的 `.layui-form-radio` 是**同一块** `.layui-input-block`
    // 里的兄弟节点 ⇒ 按「块」判，两种点击（jsdom 点原生 input / 真环境点渲染件）都覆盖。
    const block = input.closest('.layui-input-block') ?? input.parentElement
    if (block && (el === input || block.contains(el))) return true
  }
  return false
}

export async function mountSystemSettingPage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  // ⚠️ **先挂样式（`bootstrapIsland`）再塞标记**（FOUC，E-0067）；`initIslandElements`
  //    相反必须留在塞标记之后（它绑的是标记里的页签）。
  const layui: LayuiNamespace = await bootstrapIsland(['form', 'table', 'layer'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule

  document.title = TITLE
  const msg = (text: string): void => islandMsg(layer, text)
  const errText = (e: unknown): string => (e instanceof Error ? e.message : '操作失败')

  root.innerHTML = await loadConfigArtifact(ARTIFACT_KEY)
  initIslandElements(layui)

  /* ── 载入 / 勾选态 ───────────────────────────────────────────────
   *
   * 证据件里 SK32 的 `checked` 是**对标租户 suqing 的数据**（值恰好是 Y）⇒ 载入后必须用
   * 我们后端的值覆盖它（`form.val` 按 `lay-filter="sform"` 的作用域设 radio 的选中态）。
   */
  /** 后端最后已知值（载入/保存成功后更新；失败回滚的目标——⛔ 不是提交瞬间的 DOM 值，
   * 那个等于用户刚点的值，用它回滚等于没滚）。 */
  let lastLoaded: 'Y' | 'N' | null = null

  function applySk32(value: 'Y' | 'N'): void {
    ;(form.val as (filter: string, data: Record<string, unknown>) => void)(FORM_FILTER, {
      [SK32_RADIO_NAME]: value,
    })
    ;(form.render as () => void)()
  }

  function rowToValue(items: SystemSwitchRow[]): 'Y' | 'N' {
    // 按 replica_sk 找到我们那一条（今天后端只有它一条；找到就顺便核 key——
    // key 变了说明页面与后端的对应关系断了，响亮地报而不是静默存错地方）。
    const row = items.find((item) => item.replica_sk === SK32_RADIO_NAME)
    if (!row) throw new Error('系统开关列表里没有 SK32 那一条')
    if (row.key !== SK32_SWITCH_KEY) {
      throw new Error(`SK32 对应的开关 key 变了：${row.key}`)
    }
    return row.value
  }

  function reload(): void {
    void fetchSystemSwitches().then(
      (data) => {
        const value = rowToValue(data.items)
        lastLoaded = value
        applySk32(value)
      },
      (e: unknown) => msg(errText(e)),
    )
  }

  /* ── 保存（对标 `form.on('submit(save2btn)')`）─────────────────────
   *
   * 只取 SK32 的值：优先 `data.field.SK32`（真 layui 会带上整表单 60+ 个键，我们只读
   * 这一个），DOM 的选中态兜底（同一格的同一个值；与 autodiscernset「从 DOM 读」同一条
   * 纪律，这里表单没被搬出 `<form>`，两处取值语义相同）。
   * 成功：`layer.msg`（对标行为）+ **重取一次**刷新勾选态（island 里不用 location.reload）。
   * 失败：原样弹后端 `msg` + 勾选态回滚到**后端最后已知值**（`lastLoaded`）。
   */
  async function submit(field: Record<string, unknown> | undefined): Promise<void> {
    const fromField = field?.[SK32_RADIO_NAME]
    const value =
      fromField === 'Y' || fromField === 'N' ? fromField : checkedSk32Value(root)
    if (value === null) {
      msg('请先选择「是否不允许录入相同来源单号」')
      return
    }
    try {
      await updateSystemSwitch(SK32_SWITCH_KEY, value)
    } catch (e) {
      msg(errText(e))
      // 回滚到后端最后已知值：不回滚会把「没保存上」伪装成「已保存」
      // （勾选态是页面上唯一的反馈面）。
      if (lastLoaded !== null && lastLoaded !== value) applySk32(lastLoaded)
      return
    }
    msg('保存成功')
    reload()
  }

  /* ── 表格（藏第 5 个页签里，默认不可见；声明按对标原值）──────────── */
  ;(table.render as (o: unknown) => void)({
    elem: TABLE_ELEM,
    id: TABLE_ID,
    data: [],
    toolbar: '#toolbarDemo',
    // 对标 `systemsetting.js` L170（同对象里第二次赋值生效）。
    defaultToolbar: ['filter'],
    // 对标 L192-194：`page: false`（分页栏不渲染）、`limit: 10`、`limits: [10]` 逐字抄。
    page: false,
    limit: 10,
    limits: [10],
    cols: [SYSTEMSETTING_COLS],
    text: { none: '暂无数据' },
    done: () => {
      ;(form.render as () => void)()
    },
  })

  /* ── 「尚未开放」总闸（自建；`artifactPage` 那个已随本页改造消失）────
   *
   * 照抄 `artifactPage.ts` 的收口逻辑（捕获阶段、`islandTabTarget` 放行页签、
   * `clickLabel` 取名字），**多放两条路**：SK32 的 radio 组 + `save2btn`。
   * 漏了这步，页内其余 60+ 条设置会变成「点了没反应」的死控件。
   */
  const onClick = (event: Event): void => {
    const target = event.target as HTMLElement
    // 本页接真的两条路：SK32 的 radio 组与保存按钮（5 颗 `lay-filter="save2btn"` 共用）。
    if (withinSk32Block(target)) return
    if (target.closest?.('[lay-filter="save2btn"]') != null) return
    const el = target.closest?.<HTMLElement>(
      'button, a.layui-btn, .layui-form-switch, [lay-event], [lay-filter]',
    )
    if (!el || el.hasAttribute('disabled')) return
    if (islandTabTarget(target) !== null) return
    event.preventDefault()
    event.stopPropagation()
    islandMsg(layer, `「${clickLabel(el) || '该功能'}」尚未开放`)
  }
  root.addEventListener('click', onClick, true)

  /* ── 保存委托（5 颗保存按钮共用一个 filter，与对标同款）──────────── */
  ;(form.on as (
    e: string,
    cb: (data?: { field?: Record<string, unknown> }) => void,
  ) => void)(`submit(${SAVE_FILTER})`, (data) => {
    void submit(data?.field)
  })

  reload()

  return () => {
    root.removeEventListener('click', onClick, true)
    root.innerHTML = ''
  }
}

export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountSystemSettingPage(root, ctx)
}

export default { mount } satisfies IslandPage
