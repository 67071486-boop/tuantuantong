/**
 * 【人员管理 → 押金额度】iframe 子页（island key = `taker-deposit-quota`）。
 *
 * 父页：`pages/system-user-list.ts` 工具栏那颗**自有**按钮「押金额度」→
 * `layer.open({type:2, content: …page=taker-deposit-quota&uid=<id>})`。
 * 后端端点：`GET/PUT /api/business/takers/<uid>/deposit-quota/`（见 `api/takerDepositQuota.ts`）。
 *
 * ## 这是**自有功能**（对标物没有这一页）
 *
 * 所以没有证据件可对拍；口径的唯一出处是
 * `docs/plans/2026-10-23-taker-deposit-quota-plan.md` §7.2：
 *
 * - 开关「启用个人押金额度」：关 ⇒ 按全局押金额度；开 ⇒ 按下面每款游戏的个人额度。
 * - ⭐ **列表里那一列的总闸关着时本页只读**（2026-10-23 追加）：顶部红字
 *   「未启用个人押金额度，请先在列表里打开」，开关 / 输入框 / 填充按钮 / 保存**全 `disabled`**。
 *   总闸的归属在人员管理页那一列（行内开关），本页不再提供第二条打开它的路。
 * - **没填 = 0 元（该游戏免押）**——0 是合法值（E-0021 的边界），空输入框保存时归一成 `"0"`。
 * - 每款游戏一行：游戏名 / **全局额度（只读）** / 个人额度输入框（placeholder `0`）。
 * - 工具条：「统一额度 [input] 元 [一键填充]」+「[按全局额度填充]」。
 *   两者覆盖**已有非空且不同**的值时先 `layer.confirm` 确认（避免一键把手工填的值冲掉）。
 * - 保存：本地校验**非负、最多两位小数**；非法 ⇒ 提示且**不发请求**；PUT 失败**原样**显示后端 msg。
 * - 成功 ⇒ `closeIslandLayer(window)`（父页在 `layer.open({end})` 里 `reload()`）。
 * - 直接打开（无 `uid` / 加载失败）⇒ 显示错误，**不白屏**。
 *
 * ## 三条 island 硬规矩（`AGENTS.md` §5-G；本页**没有定时器**）
 *
 * ① 类名与选择器分成**两个**常量（`*_CLASS` 拼进标记、`*_SELECTOR` 查节点）；
 * ② 以 `(` 开头的语句一律前置分号；
 * ③ `await bootstrapIsland(...)` 先挂样式 → 塞 `root.innerHTML` → **之后**才 `initIslandElements`。
 * ⚠️ ①②③ 都有运行期判据（`__tests__/taker-deposit-quota.spec.ts`）。
 */
import {
  fetchDepositQuota,
  saveDepositQuota,
  type DepositQuota,
  type DepositQuotaGame,
} from '@/api/takerDepositQuota'
import { escapeHtml } from '@/layui-island/escape'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'
import { closeIslandLayer, type LayerHostWindow } from '../layerHost'

/** island key（= 文件名 = 父页 `page=` 的值，两处必须逐字一致）。 */
export const PAGE_KEY = 'taker-deposit-quota'
export const TITLE = '押金额度'

/** 页面容器的**类名**（拼标记）与**选择器**（查节点）——两者不能混（陷阱 ①）。 */
export const PANEL_CLASS = 'depositQuotaPanel'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`

/** 每款游戏那一行的类名 / 选择器（同上，两个常量）。 */
export const ROW_CLASS = 'depositQuotaRow'
export const ROW_SELECTOR = `.${ROW_CLASS}`

/** 个人额度输入框的类名 / 选择器（同上）。 */
export const AMOUNT_CLASS = 'depositQuotaAmount'
export const AMOUNT_SELECTOR = `input.${AMOUNT_CLASS}`

/** 每行承载 game_id 的属性（行与输入框都带，取值一律从输入框读）。 */
export const GAME_ID_ATTR = 'data-game-id'

/** 开关（layui `lay-skin="switch"`）的 id 与 `lay-filter`。 */
export const SWITCH_ID = 'depositQuotaEnabled'
export const SWITCH_FILTER = 'depositQuotaEnabledChecked'

/** 说明文案容器的 id（开关切换时只换这一格的文字，不重画整页）。 */
export const EXPLANATION_ID = 'depositQuotaExplanation'

/**
 * 「列表里没打开总闸」提示块的 id（2026-10-23 追加）。
 * 出现条件：`GET` 回来的 `enabled === false` ⇒ 本页变成**只读**（见 `buildHtml`）。
 */
export const DISABLED_NOTICE_ID = 'depositQuotaDisabledNotice'

/** 工具条：「统一额度」输入框 + 两颗填充按钮。 */
export const FILL_INPUT_ID = 'depositQuotaFillValue'
export const FILL_BUTTON_ID = 'depositQuotaFillBtn'
export const GLOBAL_FILL_BUTTON_ID = 'depositQuotaGlobalFillBtn'

/** 保存按钮 id。 */
export const SAVE_BUTTON_ID = 'depositQuotaSaveBtn'

/** 错误提示块的类名 / 选择器（无 uid / 加载失败时**替代**整页内容）。 */
export const ERROR_CLASS = 'depositQuotaError'
export const ERROR_SELECTOR = `.${ERROR_CLASS}`

/**
 * 用户可见文案（**纯中文、无英文兜底**）。
 * `noUid` / `loadFailed` 是「直接打开本页」这类路径的兜底——对标没有这页，也就没有原文可抄。
 */
export const DEPOSIT_QUOTA_MESSAGES = {
  /** 没有 `uid` / `uid` 不是正整数（直接打开子页的深链）。 */
  noUid: '没有要设置押金额度的人',
  /** GET 失败时的前缀（后面接后端原样 `msg`）。 */
  loadFailed: '押金额度加载失败',
  /** 统一额度输入框为空就点「一键填充」。 */
  fillEmpty: '请输入统一额度',
  /** 额度形状不合法（负数 / 超过两位小数 / 非数字）。 */
  invalidAmount: '额度必须是大于等于 0 的数字，最多两位小数',
  /** 一键填充 / 按全局填充要覆盖已有非空值时的确认。 */
  confirmOverwrite: '已填写的个人额度将被覆盖，确定继续吗？',
  /** 保存成功（父页在 `end` 里刷新列表）。 */
  saved: '押金额度已保存',
  /** PUT 失败且错误对象上没有可读消息时的兜底。 */
  saveFailed: '保存失败',
  /**
   * ⭐ 总闸关着时页面顶部的提示（业务方 2026-10-23 指定的**逐字**文案）。
   * 关着时本页所有输入 / 按钮 / 开关一律 `disabled`（⛔ 不许出现「填了半天保存不了」的静默）。
   */
  disabledNotice: '未启用个人押金额度，请先在列表里打开',
} as const

/**
 * 从 `window.location.search` 取 `uid`：**只认正整数**（island 的 `IslandContext` 不带 query）。
 *
 * 为什么不用 `Number(raw)` 直接判：`Number('')` 是 `0`、`Number(' ')` 是 `0`、
 * `Number('1.5')` 是 `1.5`——它们都会悄悄变成一个「看起来合法」的 id，然后打到
 * `/api/business/takers/0/deposit-quota/` 这种无意义请求上（E-0010 同型：请求来源的值
 * 进类型化的位置之前一律先过判据）。`/^\d+$/` + `> 0` 才是「正整数」。
 * `0` 不算：`auth.User` 的主键从 1 起。
 */
export function uidFromSearch(search: string): number | null {
  const raw = (new URLSearchParams(search).get('uid') ?? '').trim()
  if (!/^\d+$/.test(raw)) return null
  const id = Number(raw)
  return Number.isSafeInteger(id) && id > 0 ? id : null
}

/**
 * 金额形状判据：**非负、最多两位小数**（后端还有同一道闸；0 合法 = 该游戏免押）。
 *
 * ⚠️ **有意比后端更严**：后端 `Decimal` 收 `.5` / `1.` / `1e3`，这里只认「数字 + 可选两位小数」
 * （配置页的高频输入形状）。被本地拒时用户拿到的是本页提示、不是后端 msg；两边都拒的值
 * 语义一致，只是文案出处不同。
 */
export function isValidAmount(value: string): boolean {
  return /^\d+(\.\d{1,2})?$/.test(value)
}

/** 说明文案随开关变（关 = 按全局；开 = 按下面额度，没填 = 0 元免押）。 */
export function explanationText(enabled: boolean): string {
  return enabled
    ? '开启后按下面填写的个人额度冻结押金；某款游戏没填 = 0 元（该游戏免押）。'
    : '关闭时按全局押金额度（下方「全局额度」一列）；开启后按下面填写的个人额度。'
}

/**
 * 押金**闸**与个人额度是两件事：个人额度只覆盖「该冻多少」，不负责打开闸。
 * 生产两台当前两个闸都未开 ⇒ 这句静态提示必须留在页面上，否则运维配完看不到效果、
 * 也不知道原因（计划 §2.2「必须如实告知」）。
 */
export const GATE_HINT_TEXT =
  '提示：只有该租户 / 该客户端的押金闸已打开时，这里的额度才会真正冻结押金；闸关着时谁都不冻。'

/**
 * 一行游戏的标记（游戏名等外部值一律 `escapeHtml`；`amount=null` ⇒ 空输入框 + placeholder `0`）。
 *
 * `disabled = true`（总闸关着）时输入框带 `disabled`：这一页此时是**只读**的，
 * 用户改不了也存不了（保存按钮同样禁用，见 `buildHtml`）。
 */
function gameRowHtml(game: DepositQuotaGame, disabled = false): string {
  const amount = game.amount === null ? '' : escapeHtml(game.amount)
  const id = escapeHtml(String(game.game_id))
  return `<tr class="${ROW_CLASS}" ${GAME_ID_ATTR}="${id}">
          <td>${escapeHtml(game.game_name)}</td>
          <td>${escapeHtml(game.global_amount)} 元</td>
          <td><input class="layui-input ${AMOUNT_CLASS}" ${GAME_ID_ATTR}="${id}" type="text" value="${amount}" placeholder="0" autocomplete="off"${disabled ? ' disabled' : ''}></td>
        </tr>`
}

/**
 * 整页标记（自有功能，没有证据件；结构按计划 §7.2）。
 *
 * 外面包 `<form class="layui-form">`：layui 的开关（`lay-skin="switch"`）要被 `form.render()`
 * 认出来，必须住在 `.layui-form` 里。按钮全是 `type="button"`，不会提交表单。
 *
 * ⭐ **总闸关着（`quota.enabled === false`）⇒ 整页只读**（2026-10-23 追加）：
 * 顶部一行红字「未启用个人押金额度，请先在列表里打开」，开关 / 统一额度输入框 /
 * 两颗填充按钮 / 每行输入框 / 保存按钮**全部 `disabled`**。
 * 为什么不是「保存时再拒」：业务方要的是「关着就不能配」——让人填完一整页再告诉他
 * 「保存不了」是把规则藏到最后（§5-C 禁止的静默误导）。要用就先回列表打开那一列。
 * 判据（能红）：`taker-deposit-quota.spec.ts` 的「关着 ⇒ 提示 + 全部控件 disabled + 点了不发请求」。
 */
export function buildHtml(quota: DepositQuota): string {
  const disabled = !quota.enabled
  const disabledAttr = disabled ? ' disabled' : ''
  const rows = quota.games.map((game) => gameRowHtml(game, disabled)).join('\n        ')
  return `
<style type="text/css">
  body {
    background-color: #fff
  }
</style>
<form class="layui-form" action="" onsubmit="return false">
  <div class="${PANEL_CLASS}" style="padding:10px 15px">
    ${
      disabled
        ? `<div id="${DISABLED_NOTICE_ID}" style="margin:0 0 8px 0;color:#FF5722">${DEPOSIT_QUOTA_MESSAGES.disabledNotice}</div>`
        : ''
    }
    <div style="margin:0 0 6px 0">
      <span style="margin-right:8px">启用个人押金额度</span>
      <input type="checkbox" id="${SWITCH_ID}" lay-skin="switch" lay-text="启用|关闭" lay-filter="${SWITCH_FILTER}"${
        quota.enabled ? ' checked' : ''
      }${disabledAttr}>
    </div>
    <div id="${EXPLANATION_ID}" style="margin:0 0 8px 0;color:#666">${explanationText(quota.enabled)}</div>
    <div style="margin:0 0 8px 0;color:#999;font-size:12px">${GATE_HINT_TEXT}</div>
    <div style="margin:0 0 8px 0">正在配置：${escapeHtml(quota.display_name)}（${escapeHtml(quota.username)}）</div>
    <div style="margin:0 0 8px 0">
      统一额度
      <input class="layui-input" id="${FILL_INPUT_ID}" type="text" placeholder="0" autocomplete="off" style="display:inline-block;width:100px;margin:0 4px"${disabledAttr}>
      元
      <button class="layui-btn layui-btn-xs" id="${FILL_BUTTON_ID}" type="button"${disabledAttr}>一键填充</button>
      <button class="layui-btn layui-btn-xs layui-btn-normal" id="${GLOBAL_FILL_BUTTON_ID}" type="button"${disabledAttr}>按全局额度填充</button>
    </div>
    <table class="layui-table" lay-size="sm">
      <thead>
        <tr><th style="width:40%">游戏</th><th style="width:30%">全局额度</th><th style="width:30%">个人额度</th></tr>
      </thead>
      <tbody>
        ${rows}
      </tbody>
    </table>
    <div style="text-align:center;margin-top:10px">
      <button class="layui-btn" id="${SAVE_BUTTON_ID}" type="button"${disabledAttr}>保存</button>
    </div>
  </div>
</form>`
}

/** 错误态的整页标记（无 uid / 加载失败）：**不白屏**，把原因写清楚。 */
export function buildErrorHtml(message: string): string {
  return `<div class="${PANEL_CLASS}" style="padding:10px 15px">
  <div class="${ERROR_CLASS}" style="color:#FF5722;padding:20px 0">${escapeHtml(message)}</div>
</div>`
}

/** 挂载页面。返回卸载函数（切路由 / 关弹层时调用）。 */
export async function mountTakerDepositQuotaPage(root: HTMLElement): Promise<() => void> {
  // 陷阱 ③：先挂样式（layui.css / vendor / 覆盖样式），后面才能塞标记。
  const layui: LayuiNamespace = await bootstrapIsland(['form', 'layer'])
  const form = layui.form as LayuiModule
  const layer = layui.layer as LayuiModule
  const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

  // 页标题：本页是自有页面（没有对标的服务端 `<title>` 可抄），island 外壳只有全站共用的
  // `<title>团团通</title>` ⇒ 每个页面模块挂载时自己设（同 `system-setgames.ts`）。
  document.title = TITLE

  const uid = uidFromSearch(window.location.search)
  if (uid === null) {
    // 直连深链（没有 `uid` / 传了非正整数）：提示 + 显示错误，**一个请求都不发**。
    root.innerHTML = buildErrorHtml(DEPOSIT_QUOTA_MESSAGES.noUid)
    msg(DEPOSIT_QUOTA_MESSAGES.noUid)
    return () => {
      root.innerHTML = ''
    }
  }

  // `uid` 在上面已经过 `!== null` 的闸；这里取一个**非空**的局部常量再交给闭包——
  // `save()` 是**函数声明**（会提升到闸之前），TS 不会把外层的收窄带进它的函数体
  // （TS2345：`number | null` 不能传给 `number`）。这不是运行时兜底，只是把同一份判据写明。
  const userId: number = uid

  let quota: DepositQuota
  try {
    quota = await fetchDepositQuota(userId)
  } catch (e) {
    // 失败**不吞**：页面显示「加载失败 + 后端原样 msg」，用户知道发生了什么（不是白屏）。
    const detail = e instanceof Error && e.message.trim() !== '' ? e.message : ''
    root.innerHTML = buildErrorHtml(
      detail === ''
        ? DEPOSIT_QUOTA_MESSAGES.loadFailed
        : `${DEPOSIT_QUOTA_MESSAGES.loadFailed}：${detail}`,
    )
    return () => {
      root.innerHTML = ''
    }
  }

  root.innerHTML = buildHtml(quota)
  // 陷阱 ③的后半：**标记已在**才初始化 layui（开关要靠 `form.render()` 才画成 switch）。
  initIslandElements(layui)
  ;(form.render as (type?: string) => void)()

  const amountInputs = (): HTMLInputElement[] =>
    Array.from(root.querySelectorAll<HTMLInputElement>(AMOUNT_SELECTOR))

  const gameIdOf = (input: HTMLInputElement): number => Number(input.getAttribute(GAME_ID_ATTR) ?? '')

  const switchInput = (): HTMLInputElement | null =>
    root.querySelector<HTMLInputElement>(`#${SWITCH_ID}`)

  const setExplanation = (enabled: boolean): void => {
    const el = root.querySelector<HTMLElement>(`#${EXPLANATION_ID}`)
    if (el) el.textContent = explanationText(enabled)
  }

  // 开关切换只换说明文案（关 = 按全局 / 开 = 按下面额度）。
  // 走 `form.on('switch(filter)')` 而不是原生 `change`：layui 的 switch 是**渲染出来的**，
  // 真浏览器里点的是它画的那个 div，原生 change 不一定发；`form.on` 才是 layui 的出口。
  ;(form.on as (e: string, cb: (obj: { elem?: HTMLInputElement }) => void) => void)(
    `switch(${SWITCH_FILTER})`,
    (obj) => {
      setExplanation(obj?.elem?.checked === true)
    },
  )

  /**
   * 把 `values` 写进对应输入框；**覆盖已有非空且不同**的值之前先确认（计划 §7.2）。
   * 确认只在有冲突时弹一次（不是每行一次）。
   */
  function applyValues(values: ReadonlyMap<number, string>): void {
    const inputs = amountInputs()
    const conflicts = inputs.filter((input) => {
      const next = values.get(gameIdOf(input))
      const current = input.value.trim()
      return next !== undefined && current !== '' && current !== next
    })
    const apply = (): void => {
      for (const input of inputs) {
        const next = values.get(gameIdOf(input))
        if (next !== undefined) input.value = next
      }
    }
    if (conflicts.length === 0) {
      apply()
      return
    }
    ;(layer.confirm as (t: string, o: unknown, cb: (i: number) => void) => void)(
      `${DEPOSIT_QUOTA_MESSAGES.confirmOverwrite}（${conflicts.length} 款游戏的个人额度会被覆盖）`,
      { title: '确认覆盖' },
      (index: number) => {
        ;(layer.close as (i: number) => void)(index)
        apply()
      },
    )
  }

  /** 「一键填充」：把统一额度写进**所有**输入框；空 / 非法 ⇒ 提示且不动任何输入框。 */
  const onFillUniform = (): void => {
    const raw = (root.querySelector<HTMLInputElement>(`#${FILL_INPUT_ID}`)?.value ?? '').trim()
    if (raw === '') {
      msg(DEPOSIT_QUOTA_MESSAGES.fillEmpty)
      return
    }
    if (!isValidAmount(raw)) {
      msg(DEPOSIT_QUOTA_MESSAGES.invalidAmount)
      return
    }
    applyValues(new Map(amountInputs().map((input) => [gameIdOf(input), raw])))
  }

  /** 「按全局额度填充」：每款游戏把 `global_amount` 写进对应输入框。 */
  const onFillGlobal = (): void => {
    applyValues(new Map(quota.games.map((g) => [g.game_id, g.global_amount])))
  }

  let saving = false

  /** 保存：收集开关 + 每个输入框（空 ⇒ `"0"`）→ 本地校验 → PUT。 */
  async function save(): Promise<void> {
    if (saving) return
    const inputs = amountInputs()
    const amounts: Record<string, string> = {}
    for (const input of inputs) {
      const raw = input.value.trim()
      const value = raw === '' ? '0' : raw
      if (!isValidAmount(value)) {
        msg(DEPOSIT_QUOTA_MESSAGES.invalidAmount)
        return
      }
      amounts[String(gameIdOf(input))] = value
    }
    const enabled = switchInput()?.checked === true
    saving = true
    try {
      await saveDepositQuota(userId, { enabled, amounts })
    } catch (e) {
      // 失败**原样**显示后端 msg（不翻译、不减字），并**不关**弹层——让用户能改一改重试。
      msg(
        e instanceof Error && e.message.trim() !== ''
          ? e.message
          : DEPOSIT_QUOTA_MESSAGES.saveFailed,
      )
      return
    } finally {
      saving = false
    }
    msg(DEPOSIT_QUOTA_MESSAGES.saved)
    // 关掉承载自己的那个弹层；父页的列表刷新挂在 `layer.open({end})` 上（见父页
    // `DEPOSIT_QUOTA_DIALOG_BASE` 的注释）。⛔ 不新增 postMessage 协议。
    closeIslandLayer(window as unknown as LayerHostWindow)
  }

  root.querySelector<HTMLButtonElement>(`#${FILL_BUTTON_ID}`)?.addEventListener('click', onFillUniform)
  root
    .querySelector<HTMLButtonElement>(`#${GLOBAL_FILL_BUTTON_ID}`)
    ?.addEventListener('click', onFillGlobal)
  root.querySelector<HTMLButtonElement>(`#${SAVE_BUTTON_ID}`)?.addEventListener('click', () => {
    void save()
  })

  return () => {
    root.innerHTML = ''
  }
}

/** 标准 island 页出口（`layui-island/page.ts` 的注册表按它调用）。本页不发起页面跳转。 */
export function mount(root: HTMLElement): Promise<() => void> {
  return mountTakerDepositQuotaPage(root)
}

/** 注册表按 `import.meta.glob('./pages/*.ts')` 的**默认导出**取页面。 */
export default { mount } satisfies IslandPage
