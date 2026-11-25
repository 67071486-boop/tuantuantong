/**
 * 【设置游戏权限】页的 **layui island 实现**（对标 `/User/SetGames?id=<uid>`）。
 *
 * ⚠️ 对标里这一项**不是弹层**，而是一个**独立的 iframe 页面**：
 * 父页（`/User/userlist`）用 `layer.open({type:2, content:'/User/SetGames?id=<uid>'})` 打开它
 * （我方父页见 `pages/system-user-list.ts` 的 `SET_GAMES_DIALOG_BASE`）。
 * 业务方 2026-09-22 的原话是「设置游戏权限和业务类型的弹窗没有复刻成功」——
 * 根因就是旧实现把它做成了自造的通用多选弹层，而不是这个子页。
 *
 * 证据（**只读**，快照在 `.tools/recon/sysuser/`）：
 *  - 页面原文：`User_SetGames.html`（本页很短，整份读完再写）；
 *  - 页面 JS：`assets_setgames/JS2019/User/setgames.js`。
 *
 * 页面 key 就是**文件名**：`system-setgames`（`layui-island/page.ts` 用 `import.meta.glob` 自动发现），
 * 父页拼的 URL 必须与它逐字一致。
 *
 * ## 与对标**有意不同**的四条（每条都有理由，不是笔误）
 *
 * 1. **伪选项「全部游戏」**（推断级）：对标把 `全部游戏` 当成一个普通 checkbox 值
 *    （`value="全部游戏"`）交给服务端，服务端拿到它之后怎么处理**我们没实测到**
 *    （证据分级：**推断**——对标源码 `setgames.js` 只把它当一个普通值 `join(',')` 发出去，
 *    服务端行为不可见）。我方没有这个伪游戏，于是自己定义它的语义：
 *    **勾它 = 勾上当前租户的全部游戏，取消它 = 全部取消**。
 * 2. **回填的是「这个人真实的游戏」**：抓到的对标实例（uid=622）把所有游戏都打了 `checked`，
 *    无法区分「此人确实有全部权限」还是「服务端一律全勾」⇒ 我方按真实数据回填（有意更正确）：
 *    单人 ⇒ 回填该用户的游戏；多人 ⇒ **只有所有人的游戏集合完全一致时才回填，否则不预选**
 *    （沿用本线 `system-user-list.ts` 里 `commonBusinessType` 的同一口径；那个函数随 Task 4
 *    删掉了，口径原样搬进 `commonIds()`）。
 * 3. **落库走我方 REST**：对标 POST `/UserAjax/SetUserGames`；我方**没有**批量端点，
 *    按账号线既有裁决「对选中行逐个 PATCH」，写门由后端 `system_user.add_edit` 承担。
 * 4. **关掉自己并让父页刷新**：对标是 `parent.tools.reloadOrderData()`（它父页有那个自定义工具），
 *    我方父页没有 ⇒ 用标准 layui iframe 关闭法
 *    `parent.layer.close(parent.layer.getFrameIndex(window.name))`，**由父页**
 *    在 `layer.open({type:2, end: () => reload()})` 的 `end` 回调里刷新列表。
 *    （这样不需要新增 `postMessage` 协议；父页那段实参见 `SET_GAMES_DIALOG_BASE`。）
 *
 * ## 三条 island 硬规矩（`AGENTS.md` §5）
 *
 * ① 类名与选择器分成两个常量；② 以 `(` 开头的语句前置分号；③ **先 `await bootstrapIsland(...)`
 * 挂样式、再塞 `root.innerHTML`**（兜底网：`layui-island/__tests__/page-mount-order.spec.ts`）。
 */
import { getAccount, updateAccount } from '@/api/accounts'
import { fetchAllGames, type ConfigRow } from '@/api/config'
import { escapeHtml } from '@/layui-island/escape'
import { bootstrapIsland, type IslandContext, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'
// 关弹层的共享工具（Task 13 从本文件搬到 `layui-island/layerHost.ts`：`pages/user-adduser.ts`
// 也要用它判「我在不在弹层里」）。本页既**调用**它、又**re-export** 它，见下面那段注释。
import { closeIslandLayer, type LayerHostWindow } from '../layerHost'

/** island 页 key ＝ 文件名（父页拼 URL 用；`system-user-list.spec.ts` 会钉住它）。 */
export const SET_GAMES_PAGE_KEY = 'system-setgames'

/** 页标题：对标 `User_SetGames.html` L5 的 `<title>设置游戏权限</title>`（**服务端渲染**的那个）。 */
export const SET_GAMES_TITLE = '设置游戏权限'

/**
 * 伪选项的取值（**逐字**取对标原文的 `value="全部游戏"`）。
 * ⚠️ 它不是租户里的任何一款游戏，只是「全选」开关——语义见文件头「有意不同的四条」第 1 条。
 */
export const ALL_GAMES_VALUE = '全部游戏'

/**
 * 几句用户可见文案，**逐字**取自对标 `setgames.js`（出处逐条标注）。
 * 抽成常量只为让 spec 能钉住字面值（改一个字就红）。
 */
export const SET_GAMES_MESSAGES = {
  /** `setgames.js:29`：一个游戏都没勾时的提示。 */
  noGame: '至少选择一款游戏',
  /** `setgames.js:33`：没选设置方式时的提示。 */
  noMode: '请选择设置方式',
  /** 我方补充（对标的成功文案来自服务端 `data.msg`，我方 REST 不回这种文案）。 */
  saved: '已保存',
  /** 我方补充：直接打开本页（deep-link）而没有 `uid` 时的提示——对标不存在这条路径。 */
  noAccount: '没有要设置的账号',
} as const

/**
 * `closeIslandLayer` / `LayerHostWindow` 已搬到 `layui-island/layerHost.ts`，
 * 这里保留**同名 re-export**：既有 `__tests__/system-setgames.spec.ts` 的 import 一个字都不用改。
 *
 * ⚠️ 必须**既 `import` 又「导出局部绑定」**，不能只写 `export { … } from '../layerHost'`：
 * `export … from` 只是转发语句，**不会**在本模块里建局部绑定，而本页自己也要调
 * `closeIslandLayer`（保存成功后关掉自己）——只写转发会变成 `closeIslandLayer is not defined`。
 */
export { closeIslandLayer, type LayerHostWindow }

/**
 * 从 `window.location.search` 取 `uid`（island 的 `IslandContext` **不带** query 参数）。
 * 多个人时对标传的是**逗号串**（`?uid=1,2,3`）。
 *
 * 非法段**丢掉**而不是变成 `NaN`：`Number('x')` 会产出 `NaN`，拼进 `/api/accounts/NaN/` 就是一次
 * 无意义的 404（E-0010 同型：请求来源的值进类型化的位置之前一律先过判据）。
 */
export function uidsFromSearch(search: string): number[] {
  return (new URLSearchParams(search).get('uid') ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter((s) => /^\d+$/.test(s))
    .map(Number)
}

/**
 * 「这些人的游戏 id 集合」——**全都一样才给值**，否则 `undefined`；`rows` 为空也给 `undefined`。
 *
 * 口径原样搬自 `system-user-list.ts` 里被 Task 4 删掉的 `commonBusinessType`（见 git 历史）：
 * 用途只有一个——给回填做判据，不一致时不预选，避免把「A 的游戏」悄悄显示成「A+B 的共同值」。
 * ⚠️ 单人且集合为空 ⇒ 返回 `[]`（**不是** `undefined`）：那是「这个人确实一款游戏都没有」的
 * 真实数据，回填就是「一个都不勾」；用真值判断会把 `[]` 与「没有共同值」混成一回事（E-0021 同型）。
 * 比较用**排序后的 id 串**：行内的顺序不参与判据（顺序不同但集合相同 = 一致）。
 */
export function commonIds(rows: readonly (readonly number[])[]): number[] | undefined {
  if (rows.length === 0) return undefined
  const first = rows[0]!
  const key = (ids: readonly number[]): string => [...new Set(ids)].sort((a, b) => a - b).join(',')
  const firstKey = key(first)
  return rows.every((ids) => key(ids) === firstKey) ? [...first] : undefined
}

/**
 * 并集（`current ∪ selected`），**当前集合在前的顺序**稳定——`setMode=1` 的载荷靠它可断言。
 * 去重，但不排序：排序会让「原本就有这个游戏权限时保持原样」这条语义看起来像改动过顺序。
 */
export function unionIds(current: readonly number[], selected: readonly number[]): number[] {
  return [...new Set([...current, ...selected])]
}

/**
 * 游戏复选框（含首位的伪选项「全部游戏」）。
 *
 * `value` 与 `title` 都是**游戏名**（**不是 id**）——对标原文如此
 * （`User_SetGames.html` L57-71：`value="原神" title="原神"`），父页/后端都按名字对。
 * 名字来自租户数据 ⇒ 一律过 `escapeHtml`（存储型 XSS 的入口，见 `layui-island/escape.ts`）。
 *
 * ⚠️ 伪选项**永不**回填：它不是任何人的游戏（`preselected` 里没有它才对）。
 */
export function gamesCheckboxesMarkup(
  games: readonly ConfigRow[],
  preselected: ReadonlySet<string>,
): string {
  // ⚠️ 伪选项**显式**排除在回填之外（不是「调用方不会传它」这种口头约定）：
  // 「全部游戏」不是任何人的游戏权限，勾上它意味着「全选」，与「这个人真实拥有哪些游戏」无关。
  // 属性顺序照对标原文（`User_SetGames.html` L57）：type → name → value → checked → title → lay-*。
  const box = (value: string): string =>
    `<input type="checkbox" name="games" value="${escapeHtml(value)}"${
      value !== ALL_GAMES_VALUE && preselected.has(value) ? ' checked="checked"' : ''
    } title="${escapeHtml(value)}" lay-filter="selectGame" lay-skin="tag" />`
  return [ALL_GAMES_VALUE, ...games.map((g) => g.name)].map(box).join('\n                        ')
}

/**
 * 两个「设置方式」radio（**逐字**取自对标 `User_SetGames.html` L78-85）。
 *
 * ⚠️ 两处**故意照抄**：`title=""`（空值不是没有属性）与两个 radio **都不带 `checked`**。
 * 不预选是**有意的**：`setMode` 决定的是「覆盖」还是「新增」，预选任意一个都会让「没选就保存」
 * 变成一次用户没表达过的语义（覆盖会**删掉**存量权限，方向最危险）——由 `SET_GAMES_MESSAGES.noMode`
 * 拦住「没选就保存」（对标原文同样没有默认值）。
 * `lay-tips` 是 layuiadmin 扩展的属性（我们 vendored 的 layui 里不生效），但**逐字照抄**——
 * 同页 `system-user-list.ts` 的 `cardDialogArgs` 等处已有同样处置。
 */
export function setModeRadiosMarkup(): string {
  return [
    '<input type="radio" name="setMode" value="0" title="" />',
    '<div lay-radio><span lay-tips="将这些子账号的游戏权限全都设置成你选择的游戏">统一更换成所勾选的游戏</span></div>',
    '<input type="radio" name="setMode" value="1" title="" />',
    '<div lay-radio><span lay-tips="在现在游戏权限的基础上，新增你所选择的游戏。如果原本就有这个游戏权限，将忽略">在现有基础上新增勾选的游戏</span></div>',
  ].join('\n                ')
}

/**
 * 整页标记。**逐块**照对标 `User_SetGames.html` L52-98（结构 / 类名 / 内联 style / 属性）：
 * `<form class="layui-form">` → `.layui-field-box` → 两个 `.layui-form-item`
 * → 居中的保存按钮 → 隐藏域 `#uid`。
 *
 * 三处**照抄原文**的细节：
 * ① 页内 `<style>` 只有一条 `body{background-color:#fff}`（对标 L24-27；`bootstrapIsland` 的
 *    全局覆盖里有同值规则，这里仍然照抄——判据是「页内 `<style>` 在文档顺序上晚于全局」）；
 * ② 保存按钮的 `style=""`（**空值，不是没有属性**）与 `disabled="disabled"` 都是对标原文；
 * ③ `.layui-field-box` 的内联 `padding: 0px 0px`（两个 `0px`，不是 `0`）。
 */
export function setGamesFormHtml(
  games: readonly ConfigRow[],
  uidRaw: string,
  preselected: ReadonlySet<string>,
): string {
  return `
<style type="text/css">
    body {
        background-color: #fff
    }
</style>
<form class="layui-form" action="" style="margin-left:10px;margin-right:10px">
    <div class="layui-field-box" style="margin-left:0px;padding: 0px 0px;margin-top:10px">
        <div class="layui-form-item">
            <label class="layui-form-label">勾选游戏</label>
            <div class="layui-input-block">
                        ${gamesCheckboxesMarkup(games, preselected)}
            </div>
        </div>
        <div class="layui-form-item">
            <label class="layui-form-label">设置方式</label>
            <div class="layui-input-block">
                ${setModeRadiosMarkup()}
            </div>
        </div>
    </div>

    <div style="text-align:center;align-items:center">
        <div class="layui-input-inline">
            <button class="layui-btn layui-btn-normal w-lg layui-disabled" id="saveBtn" lay-submit="" type="button" lay-filter="saveBtn" style="" disabled="disabled">保存设定</button>
        </div>
    </div>

    <input type="hidden" id="uid" name="uid" value="${escapeHtml(uidRaw)}" />
</form>`
}

/**
 * 挂载页面。返回卸载函数（切路由时调用）。
 *
 * 挂载顺序：**先 `bootstrapIsland`（挂样式）→ 取数 → 塞标记 → `form.render()` → 绑事件**。
 * 最后一步不能少：markup 是运行期插进 DOM 的，layui 的 `form` 模块不会自己重扫
 * （同仓先例 `system-roleauthority-edit.ts` 也在塞完标记后调 `form.render()`）。
 */
export async function mountSetGamesPage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  const layui: LayuiNamespace = await bootstrapIsland(['form', 'layer'])
  const form = layui.form as LayuiModule
  const layer = layui.layer as LayuiModule

  // `uid` 只能从 URL 读（`IslandContext` 只带 `onNavigate`）。
  const uids = uidsFromSearch(window.location.search)
  // ⚠️ 取**全量**（`fetchAllGames`）而不是按权限收窄的 `fetchGames()`：本页**就是配置面本身**
  // （「设置游戏权限」子页），勾选清单一旦被收窄，就会出现「看不到没勾的游戏 ⇒ 也就勾不上」
  // 的死锁（本线 DoD #4）。与账号编辑页（`user-adduser.ts`）同一口径。
  const games = await fetchAllGames()

  // 每个 uid 的**当前**游戏 id：回填与 `setMode=1` 的并集都要用，所以在挂载时一次取齐
  // （不让保存路径再取一遍——那会让「并集」依赖两次读取之间可能变化的数据）。
  // ⚠️ 读取失败**不吞**：吞掉会让 `setMode=1` 拿空集求并集 ⇒ **静默删掉**该用户已有的游戏权限
  // （E-0027 同型：写路径依赖的读取绝不能降级成空值）。让 `layui-island/page.ts` 把错误显示出来。
  const currentGames = new Map<number, number[]>()
  await Promise.all(
    uids.map(async (id) => {
      const row = await getAccount(id)
      currentGames.set(id, row.games ?? [])
    }),
  )

  const nameById = new Map(games.map((g) => [g.id, g.name]))
  const common = commonIds(uids.map((id) => currentGames.get(id) ?? []))
  const preselectedNames = new Set(
    (common ?? []).map((id) => nameById.get(id)).filter((n): n is string => n !== undefined),
  )

  // 页标题：对标是**服务端渲染**的 `<title>设置游戏权限</title>`；island 外壳只有一个全站共用的
  // `<title>团团通</title>`，所以每个页面模块挂载时自己设。
  document.title = SET_GAMES_TITLE
  // 对标 `<body style="font-size:12px;" id="iosiframe">`：`font-size` 由 `bootstrapIsland` 补在
  // body 的行内样式上（见 `layui/island.ts` 的 `applyBodyInlineStyle`），这里补 id。
  // 卸载时还原，避免同一文档被别的页复用时带着一个来历不明的 id。
  const hadBodyId = document.body.hasAttribute('id')
  if (!hadBodyId) document.body.id = 'iosiframe'

  root.innerHTML = setGamesFormHtml(games, uids.join(','), preselectedNames)
  // ⚠️ **不带类型**调 `form.render()`：传 `'checkbox'` 时 layui **只**渲染复选框，两个 `setMode`
  // radio 会停在原生 `<input type="radio">`（被 layui.css 藏起来）⇒「设置方式」**点不了**，
  // 页面在真浏览器里是坏的（2026-09-22 实跑 Chromium 抓到，原始输出见 task-6-report.md）。
  // jsdom 里的假 layui 抓不到「渲染器没被正确调用」，所以 spec 额外钉住「有一次**无参** render」。
  ;(form.render as (type?: string) => void)()

  const saveBtn = root.querySelector<HTMLButtonElement>('#saveBtn')
  const msg = (text: string): void => {
    ;(layer.msg as (t: string) => void)(text)
  }

  /** 对标 `setgames.js:18-19`：`removeClass('layui-disabled')` + `removeAttr('disabled')`。 */
  const enableSave = (): void => {
    saveBtn?.classList.remove('layui-disabled')
    saveBtn?.removeAttribute('disabled')
  }

  const gameBoxes = (): HTMLInputElement[] =>
    Array.from(root.querySelectorAll<HTMLInputElement>('input[name="games"]'))

  /** 勾选的游戏 id（伪选项展开成该租户的全部游戏 id）。 */
  const selectedGameIds = (): number[] => {
    const checked = Array.from(root.querySelectorAll<HTMLInputElement>('input[name="games"]:checked'))
    if (checked.some((b) => b.value === ALL_GAMES_VALUE)) return games.map((g) => g.id)
    const idByName = new Map(games.map((g) => [g.name, g.id]))
    return checked
      .map((b) => idByName.get(b.value))
      .filter((id): id is number => id !== undefined)
  }

  /** 选中的设置方式；没选 ⇒ 空串（对标 `setMode == null || undefined || ''` 判的就是这个）。 */
  const selectedSetMode = (): string =>
    root.querySelector<HTMLInputElement>('input[name="setMode"]:checked')?.value ?? ''

  let inflight = false

  /** 逐个 uid PATCH（我方没有批量端点；账号线既有裁决就是「对选中行逐个 PATCH」）。 */
  async function save(mode: string, selectedIds: number[]): Promise<void> {
    try {
      for (const uid of uids) {
        const payload =
          mode === '1' ? unionIds(currentGames.get(uid) ?? [], selectedIds) : selectedIds
        await updateAccount(uid, { games: payload })
      }
    } catch (e) {
      // 失败**不关**弹层：让用户能改一改重试（`end` 只在真的关掉时才刷新父页列表）。
      msg(e instanceof Error ? e.message : '保存失败')
      return
    }
    // 成功后先出提示、**再**由回调关掉自己：立刻关会把这条提示一起销毁（提示住在本页文档里）。
    // 父页的列表刷新挂在 `layer.open({..., end})` 上，所以「关闭」本身就是「刷新」的触发点
    // （对标是 `parent.tools.reloadOrderData()`，我方没有那个自定义工具，见文件头第 4 条）。
    ;(layer.msg as (t: string, o: unknown, cb: () => void) => void)(
      SET_GAMES_MESSAGES.saved,
      { time: 1000 },
      () => {
        closeIslandLayer(window as unknown as LayerHostWindow)
      },
    )
  }

  // 对标 `setgames.js:17-20`：**任何**一次游戏勾选都解开保存按钮（不判断勾了几个）。
  // ⚠️ 这条注册**不能少**：少了它保存按钮永远是灰的，而 `typecheck` / `lint` 都看不见。
  ;(form.on as (e: string, cb: (obj: { elem?: HTMLInputElement }) => void) => void)(
    'checkbox(selectGame)',
    (obj) => {
      const el = obj?.elem
      // 伪选项「全部游戏」：勾它 = 勾上全部游戏，取消它 = 全部取消（我方定义的语义，见文件头第 1 条）。
      if (el && el.value === ALL_GAMES_VALUE) {
        const on = el.checked
        for (const box of gameBoxes()) box.checked = on
        // 程序化改 `checked` **不会**触发 layui 的渲染，必须重画一次 tag 皮肤。
        ;(form.render as (type?: string) => void)('checkbox')
      }
      enableSave()
    },
  )

  ;(form.on as (e: string, cb: () => boolean) => void)('submit(saveBtn)', () => {
    const selectedIds = selectedGameIds()
    if (selectedIds.length < 1) {
      msg(SET_GAMES_MESSAGES.noGame)
      return false
    }
    const mode = selectedSetMode()
    if (mode === '') {
      msg(SET_GAMES_MESSAGES.noMode)
      return false
    }
    if (uids.length === 0) {
      // 直接打开本页（deep-link）时的兜底：对标的 `uid` 恒有值，这条路径不存在。
      msg(SET_GAMES_MESSAGES.noAccount)
      return false
    }
    // 对标用一个 `re` 布尔挡连点（`setgames.js:37-39`）；这里同名同义。
    if (inflight) return false
    inflight = true
    void save(mode, selectedIds).finally(() => {
      inflight = false
    })
    return false
  })

  return () => {
    if (!hadBodyId) document.body.removeAttribute('id')
    root.innerHTML = ''
  }
}

/**
 * 标准 island 页出口（`layui-island/page.ts` 的注册表按它调用）。
 * 本页不发起任何页面跳转（`onNavigate` 用不上）。
 */
export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountSetGamesPage(root, ctx)
}

/** 注册表按 `import.meta.glob('./pages/*.ts')` 的**默认导出**取页面。 */
export default { mount } satisfies IslandPage
