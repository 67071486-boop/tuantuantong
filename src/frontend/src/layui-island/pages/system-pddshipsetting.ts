/**
 * 【其它店商平台对接】页的 **layui 复刻页**（对标物 `/System/pddshipsetting`，**证据件标记 + 真 API**）。
 *
 * 形状：**设置表单页**（功能介绍 `blockquote` + 接口量 `fieldset` + 一张店铺对接表 + 一个
 * `display:none` 的「新增/编辑对接」弹层 + 五段平台功能设置）。
 *
 * 证据（值一律从对标取，不凭记忆猜）：
 *  - 标记与页内 `<style>` → `public/replica/system-pddshipsetting.html`
 *    （源 `.tools/recon/suqing1/html/System__pddshipsetting.html`）
 *  - 列定义 → `.tools/recon/suqing1/assets/JS2019/System/pddshipsetting.js` **L50-101**
 *  - 弹层打开参数 → 同文件 **L154-177**；保存校验 → **L303-322**；删除确认语 → **L304-308**
 *  - 工具栏 / 行内按钮的 `lay-event` → 证据件 `#toolbarDemo` / `#baroperating`
 *    （`refresh` / `addBtn` / `submitEdit` / `submitDel`）
 *  - 弹层 DOM 名与「统一 AppId + 藏起 AppSecret」的形状 → spec §2.4 那张表
 *
 * ## 为什么不走 `artifactPage`（spec §7.1.1，读码证据）
 *
 * `artifactPage` 装了一个**捕获阶段**的「尚未开放」总闸（`src/layui/artifactPage.ts:269-279`：
 * `root.addEventListener('click', onClick, true)`，对 `button, a.layui-btn, .layui-form-switch,
 * [lay-event], [lay-filter]` 一律 `preventDefault + stopPropagation`），而 layui 表格自己的
 * `*[lay-event]` 委派**在冒泡阶段** ⇒ **只要还用 `artifactPage`，工具栏与行内的真回调永远收不到点击**。
 * 它的列构建器还会给没有 `templet` 的列写死 `templet: () => ''`（`artifactPage.ts:183-185`），
 * 表格 `data` 写死 `[]`（`:234`）⇒ 真数据下整张表全是空格。
 *
 * 所以本页照 `system-sourelist.ts` 的先例：用 `mountConfigLike` 自挂载（取证据件 → 注入 →
 * `table.render` 一次 → `form.render` → 回调 `onReady`），在 `onReady` 里自己挂
 * `table.on('toolbar(…)')` / `table.on('tool(…)')`、自己开弹层；**只把仍然没有后端的控件**
 * （接口量链接 / 五段平台功能设置的单选 / 「保存以上设置」/「获取授权」）留在**捕获阶段**拦截里
 * 提示「尚未开放」。
 *
 * ## 与对标的差异（每条都给理由；值纪律见 `AGENTS.md` §5）
 *
 * | # | 差异 | 理由 |
 * |---|---|---|
 * | 1 | 「接口量」那句里的剩余调用量与 `u=` 参数为空 | R8（业务方裁定）：那是**对标租户自己的**数据，置空、结构保留（spec §7.3-P4）。点击仍归「尚未开放」 |
 * | 2 | 表格 `appSecret` 列渲染空 | 我们**没有每行的 secret**（平台密钥一次都不进浏览器，spec §9-S3）。⚠️ 对标那一格**究竟显示什么未经核实**（spec §11-T7 标为**推定**：拿一个有数据的对标行看那一格的实际取值）⇒ 本轮按 brief「渲染空」落地；**不要**把「对标显示 `******`」当成事实（那是已作废的 §7.3-P1） |
 * | 3 | `appId` 列 = `platform_app_id`（平台级统一值，每行相同） | 表格列仍用**逐行**字段（`ShopBindingSerializer.platform_app_id`，spec §6.1 的 📌 更正块保留它）。对标每行也有自己的 `appId` 且恒等于它的 `defAppid`（读码 `pddshipsetting.js:20/126`）⇒ 形状一致 |
 * | 4 | 弹层 `#appId` **只读**预填平台 AppId（取自**与行解耦**的 `platform-config` 端点）；`.appSecretDiv` 恒隐藏 | **照对标**（对标预填 `defAppid`、AppSecret 那一行默认隐藏，spec §2.4）。⚠️ 来源**不是**列表第一行：零绑定时列表为空、逐行字段读不到值，而新租户恰恰最需要这个 AppId 去授权 ⇒ 走 `GET /api/business/shop-bindings/platform-config/`（spec §6.1 的 📌 更正块）。差别只在「对标还允许把 AppId 改成租户自己的」——本轮不开放那条路（P5）⇒ 平台密钥一次都不进浏览器 |
 * | 5 | 「对应订单来源」下拉与 `soureName` 列**运行期**灌数据 | 证据件里 `#tbsoure` 本来就是「首项不设置 + **运行期填的 14 个空槽**」（spec §2.4）；不灌就是 15 行空白项，而且 `order_source` 恒为 `null` ⇒ 读单（spec §6.2 按 `order_source` 找绑定）**永远找不到配置**。名称来自既有的 `api/orderSources.ts`（**同一个字典端点**，不新建后端） |
 * | 6 | 表格不带 `limit` / `pageLimits` | 对标 `page:false`（`pddshipsetting.js:103`）⇒ 分页栏不渲染，那两项是**死配置**；对标 `table.render` 里也没有它们 |
 *
 * ## PII / 凭据（红线）
 *
 * - 平台 `AGISO_APP_SECRET` **一次都不进浏览器**：本页不取、不渲染、不预填它。
 * - `access_token`（授权码）**后端永不下发字面值**（E-0253，`ShopBindingSerializer` 只写不读）：
 *   表格列渲染「已配置 / 未配置」（读 `access_token_set` 布尔）；弹层输入框只在**换码**时填，
 *   留空保存 = 不改（后端 PATCH 空串 = 不改的既有语义）。⚠️ 早先「明文回显是照对标行为」的
 *   登记随 E-0253 作废——能不下发的凭据就不下发。
 * - 证据件里**没有**内联租户数据（弹层与下拉都是空壳，行数据来自我们的 API）。
 */
import { fetchOrderSources, type OrderSourceRow } from '@/api/orderSources'
import {
  createShopBinding,
  deleteShopBinding,
  fetchPlatformConfig,
  fetchShopBindings,
  updateShopBinding,
  type ShopBindingPayload,
  type ShopBindingRow,
} from '@/api/shopBindings'
import { findPanel, mountConfigLike, openPanelLayer } from '@/layui-island/config-replica'
import { escapeHtml } from '@/layui-island/escape'
import { islandMsg, type IslandContext, type IslandPage } from '@/layui/island'

/** 证据件名（= island key = `pages/<key>.ts` 的文件名，`mountConfigLike` 按它取 HTML）。 */
export const ARTIFACT_KEY = 'system-pddshipsetting'
/** 对标页服务端渲染的 `<title>`（`System__pddshipsetting.html` L5）。 */
export const TITLE = '其它店商平台对接'
/** 对标 `table.render` 的 `elem`（`pddshipsetting.js:51`）。 */
export const TABLE_ELEM = '#tableList'
/** `table.reloadData` / `table.on` 要的是 **id**（不带 `#`），传 `#id` 是静默 no-op。 */
export const TABLE_ID = 'tableList'
/** 弹层容器：**只有 class、没有 id**（证据件 `.modifyParamDiv`，spec §2.4）⇒ `findPanel` 必须显式传它。 */
export const PANEL_SELECTOR = '.modifyParamDiv'
/**
 * 「对标有、后端没有」的控件（照渲染、点了提示「尚未开放」，**不置灰不隐藏**：D-0025 §5）。
 *
 * ⚠️ `input[type="radio"]` 与 `.layui-form-radio` 都列上：真 layui 会把单选画成
 * `.layui-form-radio` 壳（点的是壳），而测试与「未渲染」状态下点的是裸 `input`。
 * ⚠️ 依据件里的文本输入**不**在拦截集里（对标自己的总闸也不拦它们，`artifactPage.ts:272`），
 * 它们只是没有保存路径的摆设；拦了会让用户连打字都打不了。
 */
export const NOT_OPEN_SELECTOR =
  '.payBtn, [lay-filter="savebtn"], input[type="radio"], .layui-form-radio'

/** 一行的数据形状：接口字段（spread 保留）+ 对标 `cols[].field` 的名字。 */
export type BindingCellRow = ShopBindingRow & {
  /** 对标列 `platformType`（= 平台码；文案由 `templet` 从 `platform_label` 取）。 */
  platformType: number
  /** 对标列 `shopName`。 */
  shopName: string
  /** 对标列 `soureName`：**名称**（接口只给 `order_source` id，由 `toBindingRow` 解析）。 */
  soureName: string
  /** 对标列 `appId`。 */
  appId: string
  /** 对标列 `appSecret`：恒空（没有每行的 secret，见文件头差异 #2）。 */
  appSecret: string
  /**
   * 对标列 `accessToken`。⚠️ E-0253 起后端不下发授权码字面值 ⇒ 本列渲染
   * 「已配置 / 未配置」（读 `access_token_set` 布尔），不再是对标那样的明文。
   */
  accessToken: string
}

/**
 * 列骨架（顺序/标题/宽度/对齐）**逐条照 `pddshipsetting.js` L55-101**。
 *
 * ⚠️ **操作列必须挂 `toolbar: '#baroperating'`**：漏了它行内就没有「修改 / 删除」两颗按钮
 * （原来的 `artifactPage` 版本就没挂——那时表格恒空、看不出来）。
 */
export function buildPddshipsettingCols(): Record<string, unknown>[] {
  return [
    {
      field: 'platformType',
      title: '平台',
      width: 100,
      align: 'center',
      // 对标该列是 templet 函数（把平台码映射成文案）。我们有后端给的 `platform_label`，
      // 直接用它——**不再在前端维护第二份码表**（同一事实两套判据必漂移）。
      // ⚠️ `templet` 的返回值被 layui **当 HTML 插入** ⇒ 转义（`escape.ts` 的理由）。
      templet: (row: Record<string, unknown>): string =>
        `<span>${escapeHtml(String(row.platform_label ?? ''))}</span>`,
    },
    { field: 'shopName', title: '店铺ID或昵称', width: 200, align: 'center' },
    { field: 'soureName', title: '订单来源匹配', width: 200, align: 'center' },
    { field: 'appId', title: 'AppId', width: 170, align: 'left' },
    // 我们每行**没有** AppSecret：`toBindingRow` 置空（见文件头差异 #2 的「推定」标注）。
    { field: 'appSecret', title: 'AppSecret', width: 250, align: 'left' },
    // E-0253：列宽/标题照对标，但格值是「已配置 / 未配置」（授权码字面值不再下发）。
    { field: 'accessToken', title: 'AccessToken', width: 355, align: 'left' },
    { field: 'remark', title: '备注说明', align: 'left' },
    { title: '操作', width: 160, align: 'center', toolbar: '#baroperating' },
  ]
}

/**
 * 接口行 → 表格行：补上对标 `cols[].field` 的键名，并用**来源字典**把 `order_source` 的 id
 * 解析成名称（后端只回 id，见文件头差异 #5）。
 *
 * `sourceNames` 查不到时（来源被删 / 字典还没取到）退化成空串——**不**写 `null`/`undefined`
 * （那会在单元格里渲染成字面量 `undefined`）。
 */
export function toBindingRow(
  row: ShopBindingRow,
  sourceNames: ReadonlyMap<number, string>,
): BindingCellRow {
  return {
    ...row,
    platformType: row.platform,
    shopName: row.shop_name,
    soureName: row.order_source === null ? '' : (sourceNames.get(row.order_source) ?? ''),
    appId: row.platform_app_id,
    appSecret: '',
    // E-0253：接口只给布尔 ⇒ 列渲染状态文案（不再有字面值可显示）。
    accessToken: row.access_token_set ? '已配置' : '未配置',
  }
}

/**
 * 弹层打开参数——**逐项**照 `pddshipsetting.js:154-177`（spec §2.4）。
 * `type: 1` 由 `openPanelLayer` 自己补（它还要把 `content` 包成 jQuery 对象）。
 */
export function bindingDialogOptions(editing: boolean, content: unknown): Record<string, unknown> {
  return {
    title: editing ? '编辑对接' : '新增一个店铺对接',
    shade: [0.3, '#333'],
    // 对标 `shadeClose: false`（点遮罩**不**关窗）：弹层里有半填的授权码，误关代价高。
    shadeClose: false,
    skin: 'layui-layer-lan',
    anim: 8,
    // `isPc` 的三元取**桌面分支**（我们只做桌面后台，与来源管理页的 `area` 处置同例）。
    area: ['870px', '680px'],
    content,
  }
}

/**
 * 保存时 `order_source` 该发什么（见 `resolveOrderSource`）。
 *
 * 返回 `undefined` = **不带这个键**（PATCH 缺键 = 不改，`ShopBindingSerializer.update()` 同族语义；
 * POST 缺键 = 后端默认 `null`）。
 *
 * **语义（顺序是语义的一部分，别调换）**：
 *  1. `sourcesLoaded === false`（本页一次都没取到字典）：下拉里只有「不设置」⇒ `<select>.value`
 *     恒为 `''`（评审用 jsdom 探针实测 `selectedIndex = -1`）。把这个 `''` 当「用户选了不设置」
 *     发出去，就会把已有绑定**静默清成 `null`** ⇒ 不带键。
 *  2. **用户确实选了东西（`selected !== ''`）⇒ 以他的选择为准**：发那个 id。
 *     ⚠️ 这一支**必须先于**下面那条「认不出原值」的护栏：否则「已有来源本页认不出（字典陈旧 /
 *     来源数 > `page_size=200` 被截断）+ 用户主动改选了一个**认得的新来源**」会走成「不带键」
 *     ——界面提示保存成功、**来源却根本没变**（修复轮 1 引入的窄回归，修复轮 2 #1）。
 *  3. 用户没动（下拉停在「不设置」）而**原有来源本页认不出** ⇒ 下拉根本显示不出它
 *     ⇒ 不带键（保留原值；别把「显示不出」当「清空」）。
 *  4. 其余（含用户**主动**选「不设置」）⇒ 发 `null`：清空这条路必须留着
 *     （E-0021 的同族：别把空串与「没填」当同一件事）。
 *
 * 畸形值（DOM 被改 / 非数字 / `0` / 负数）既不猜也不折成 0（0 不是合法来源 id）⇒ 不带键。
 */
export function resolveOrderSource(input: {
  /** 下拉当前的值（`''` = 首项「不设置」）。 */
  selected: string
  /** 字典是否**成功取到过**（见 `sourcesLoaded`）。 */
  sourcesLoaded: boolean
  /** 当前正在编辑的行的 `order_source`（`null` = 新增 / 本来就没设）。 */
  existing: number | null
  /** 本页认得的来源 id 集合（字典成功取到的那份）。 */
  knownSourceIds: ReadonlySet<number>
}): number | null | undefined {
  if (!input.sourcesLoaded) return undefined
  if (input.selected !== '') {
    // 用户的选择优先（见上面第 2 条：这条必须排在「认不出原值」之前）。
    const picked = Number(input.selected)
    return Number.isInteger(picked) && picked > 0 ? picked : undefined
  }
  // 用户没动、而下拉显示不出原有来源（认不出）⇒ 保留原值，不带键。
  if (input.existing !== null && !input.knownSourceIds.has(input.existing)) return undefined
  return null
}

/**
 * 保存的前端校验（对标 `pddshipsetting.js:303-322` 的前两条）。
 *
 * 返回归一化后的 `platform` + `error`（`''` = 通过）——与 `system-sourelist.ts` 的
 * `validateSoureForm` 同一手法：**判据只有这一处**，调用方不再自己判断一遍。
 *
 * ⚠️ **只有「平台必选」这一条**（spec §7.1 的更正，2026-09-21 Task 6 评审）：
 *  - 对标第三条（`appSecret` 为空且 `appId != defAppid` 才算错）在本页**不可能触发**：AppId
 *    只读且恒等于平台值 ⇒ 也就是「AppSecret 不校验」。
 *  - 对标第二条「AppId 必填」**不做**：AppId 在我们这里是**只读**字段、**从不进载荷**，
 *    要租户去填一个他改不了的字段是死路。（`platform-config` 端点让弹层在**零绑定**时也能
 *    显示 AppId，但「显示得出」不等于「租户填得了」——把它改成必填仍然没有自救路径。）
 *    「平台 AppId 没配」这个事实由**读单端点的 `1015`** 表达——**同一个事实不在两处各判一次**。
 *    ⚠️ 原文那句「前端校验照对标（平台必选 + **AppId 必填**）」已按此更正，见 spec §7.1。
 */
export function validateBindingForm(input: {
  platform: number | null
}): { error: string; platform: number } {
  if (input.platform === null) {
    // 文案逐字取对标（`pddshipsetting.js:316`）。
    return { error: '请选择一个店铺所在的平台', platform: 0 }
  }
  return { error: '', platform: input.platform }
}

/**
 * 「尚未开放」提示里那个 `「X」` 的 X：三颗具名控件用固定文案，五段平台功能设置用**它所在
 * `.layui-form-item` 的说明文字**（对标那些项没有 `.layui-form-label`，说明就写在
 * `.layui-input-block` 里）。兜底「该功能」，绝不提示一句空话。
 */
export function notOpenLabel(el: HTMLElement): string {
  if (el.classList.contains('payBtn')) return '购买接口调用量'
  if (el.classList.contains('getAuth')) return '获取授权'
  if (el.getAttribute('lay-filter') === 'savebtn') return '保存以上设置'
  return (
    el.closest('.layui-form-item')?.querySelector('.layui-input-block')?.textContent?.trim() ||
    el.textContent?.trim() ||
    '该功能'
  )
}

/* ────────────────────────────── 挂载 ────────────────────────────── */

export async function mountPddshipsettingPage(
  root: HTMLElement,
  ctx: IslandContext,
): Promise<() => void> {
  return mountConfigLike(
    root,
    {
      key: ARTIFACT_KEY,
      title: TITLE,
      tableSpec: {
        elem: TABLE_ELEM,
        // `reloadData` / `table.on` 按**实例 id** 查表（`config-replica.ts:116-122`）：
        // 显式给 id 就不依赖证据件里 `<table>` 的 id 属性（少一个静默失败的入口）。
        id: TABLE_ID,
        toolbar: '#toolbarDemo',
        // 对标同一个对象里写了两次 `defaultToolbar`（`[]` → `['filter']`），**后者生效**，照抄结果值。
        defaultToolbar: ['filter'],
        // 对标 `page:false`（`pddshipsetting.js:103`）：一次取全，没有分页控件。
        page: false,
        height: 'full',
        cols: [buildPddshipsettingCols()],
        text: { none: '暂无数据' },
      },
      onReady: ({ root: el, layui, form, table, layer }) => {
        const msg = (text: string): void => islandMsg(layer, text)
        const errText = (e: unknown): string => (e instanceof Error ? e.message : '操作失败')

        /**
         * 平台级 AppId：弹层 `#appId` 的只读预填值。
         *
         * **来源与「行」解耦**：优先取 `platform-config` 端点（零绑定时也有值），
         * 接口失败或返回空串时才退回列表第一行的逐行 `platform_app_id`（见 `reload()`）。
         * ⚠️ 它**不是密钥**（`AGISO_APP_SECRET` 一次都不进浏览器，spec §9-S3）。
         */
        let platformAppId = ''
        /** 来源字典：id → 名称（`soureName` 列与弹层下拉**共用这一份**）。 */
        const sourceNames = new Map<number, string>()
        /**
         * 来源字典**是否成功取到过**。
         *
         * ⚠️ 只置真、**不复位**：取失败时下拉里保留的是**上一次成功**的那份选项（`fillSourceOptions`
         * 只在成功时被调用），所以「曾经加载过」在这之后仍然成立。它唯一为假的时段是「本页从挂载起
         * 一次都没取到」——那时下拉里只有「不设置」，`<select>.value` 恒为 `''`，
         * 而这个 `''` **不代表用户想清空**（见 `resolveOrderSource`）。
         */
        let sourcesLoaded = false
        /** 当前正在编辑的行（`null` = 新增）；不用证据件里那个隐藏的 `#ptid`（行对象是权威）。 */
        let editing: ShopBindingRow | null = null
        let layerIndex: number | null = null

        /* ── 弹层节点 ──
         *
         * `openPanelLayer` 关闭弹层时会把节点**搬回** `<form>`（它的 `end` 回调），但 layui 的
         * 表格重绘会重建工具栏那一带、可能把它连带摘掉 ⇒ 留一份**模板快照**兜底
         * （先例 `system-sourelist.ts:294-318`：没有它，「重载后再点新增」会静默打不开）。
         */
        const initialPanel = el.querySelector<HTMLElement>(PANEL_SELECTOR)
        // ⚠️ 这两条必须在**快照之前**做：快照是克隆源，`ensurePanel()` 补出来的节点也要带着它们。
        if (initialPanel) {
          const appIdInput = initialPanel.querySelector<HTMLInputElement>('#appId')
          // 「AppId 只读」（P5）：平台统一一个应用，租户不可改。
          if (appIdInput) appIdInput.readOnly = true
          const secretDiv = initialPanel.querySelector<HTMLElement>('.appSecretDiv')
          // AppSecret 那一行恒隐藏（对标形状 + 平台密钥一次都不进浏览器）。
          if (secretDiv) secretDiv.style.display = 'none'
        }
        const panelTemplate = initialPanel ? (initialPanel.cloneNode(true) as HTMLElement) : null

        const panelHost = (): HTMLElement | null =>
          el.querySelector<HTMLElement>('form.layui-form') ?? el.querySelector('form')

        /** 取回可用的弹层节点；被 layui 摘走后从快照补一个**干净副本**。 */
        function ensurePanel(): HTMLElement | null {
          const existing = findPanel(el, PANEL_SELECTOR)
          if (existing) return existing
          const host = panelHost()
          if (!host || !panelTemplate) return null
          const fresh = panelTemplate.cloneNode(true) as HTMLElement
          host.appendChild(fresh)
          return fresh
        }

        /** 弹层里的输入（按 `name` 在**弹层节点内**找，不写文档级选择器）。 */
        const input = (panel: HTMLElement | null, name: string): HTMLInputElement | null =>
          panel?.querySelector<HTMLInputElement>(`[name="${name}"]`) ?? null

        /** 当前选中的平台（`null` = 未选，第一条校验的判据）。 */
        const checkedPlatform = (panel: HTMLElement | null): number | null => {
          const el2 = panel?.querySelector<HTMLInputElement>('[name="platforms"]:checked') ?? null
          if (!el2) return null
          const value = Number(el2.value)
          return Number.isFinite(value) && value > 0 ? value : null
        }

        /** 弹层里的下拉值（`''` = 首项「不设置」）。 */
        const selectValue = (panel: HTMLElement | null, name: string): string =>
          panel?.querySelector<HTMLSelectElement>(`select[name="${name}"]`)?.value ?? ''

        /** 把来源字典灌进弹层下拉（首项「不设置」照证据件原文）。 */
        function fillSourceOptions(items: readonly OrderSourceRow[]): void {
          const select = ensurePanel()?.querySelector<HTMLSelectElement>('select#tbsoure')
          if (!select) return
          // ⚠️ `<option>` 是 innerHTML 拼的，来源名是用户输入 ⇒ 转义（`escape.ts` 的同一 sink）。
          select.innerHTML = ['<option value="">不设置</option>']
            .concat(items.map((s) => `<option value="${s.id}">${escapeHtml(s.name)}</option>`))
            .join('')
          // layui 的下拉要 `form.render('select')` 才会把选项壳重画（先例 `fillGameOptions`）。
          ;(form.render as (type?: string) => void)('select')
        }

        /**
         * 取订单来源字典。⚠️ 失败**不阻断**页面（下拉只剩「不设置」、`soureName` 列空）：
         * 它是本页的辅助数据，取不到不该让整张店铺表打不开。
         */
        async function loadSourceOptions(): Promise<void> {
          try {
            const page = await fetchOrderSources()
            sourceNames.clear()
            for (const item of page.items) sourceNames.set(item.id, item.name)
            fillSourceOptions(page.items)
            // 只有**成功**这一次才算「加载过」（失败时上面那份仍然可用，见 `sourcesLoaded` 的注释）。
            sourcesLoaded = true
          } catch {
            // 见上：静默降级（不提示——用户点开下拉看不到来源时会去「订单来源管理」建，那一步本来就要先做）。
            // ⚠️ 降级**不包括**「保存时把这个字段当空发出去」：见 `resolveOrderSource`。
          }
        }

        /**
         * 取数：`fetchShopBindings()` → `table.reloadData`。
         *
         * ⚠️ **不带 `url`**（house 口径，`config-replica.ts:81`）：统一信封、CSRF 与错误映射都在
         * `api/` 一处；而且读码可知只要 config 里还有 `url`，`reloadData(id, {data})` 就走 ajax 分支、
         * 忽略我们传的 `data`（先例 `system-sourelist.ts:448-463`）。
         */
        async function reload(): Promise<void> {
          try {
            // 字典、列表与平台配置**一起**取：`soureName` 列要用名称（后端逐行只回 `order_source` id）；
            // 平台 AppId 走与「行」解耦的 `platform-config`（零绑定时也要有值）。
            const [page, , config] = await Promise.all([
              fetchShopBindings(),
              loadSourceOptions(),
              // ⚠️ 新端点**只是预填值**：它取失败不许让整张表打不开 ⇒ 就地吞掉、退回旧路径
              //    （`.catch(() => null)` 把拒绝变成「没有值」，`Promise.all` 因此不会短路）。
              fetchPlatformConfig().catch(() => null),
            ])
            // 取值优先级：新端点的平台值（非空）⇒ 列表第一行的逐行值 ⇒ 保持旧值。
            const configured = config?.platform_app_id ?? ''
            const first = page.items[0]
            if (configured !== '') platformAppId = configured
            else if (first) platformAppId = first.platform_app_id
            ;(table.reloadData as (id: string, o: unknown) => void)(TABLE_ID, {
              data: page.items.map((row) => toBindingRow(row, sourceNames)),
            })
          } catch (e) {
            msg(errText(e))
          }
        }

        /**
         * 弹层打开。⚠️ **顺序是硬要求**：「先预填、再 open」——`layer.open({type:1, content: jq})`
         * 会把 `.modifyParamDiv` 搬出 `<form>`，而 `form.val('form', …)` 按 `<form lay-filter>`
         * 的作用域找输入 ⇒ 先 open 再预填，编辑时打开是空表单（先例 `system-sourelist.ts:337-344`）。
         */
        function openForm(row: ShopBindingRow | null): void {
          editing = row
          if (layerIndex !== null) {
            ;(layer.close as (i: number) => void)(layerIndex)
            layerIndex = null
          }
          const panel = ensurePanel()
          if (!panel) {
            // 节点与快照都没有：只提示，不假装打开了（绝不静默）。
            msg('弹窗内容缺失，请联系管理员')
            return
          }
          const secretDiv = panel.querySelector<HTMLElement>('.appSecretDiv')
          if (secretDiv) secretDiv.style.display = 'none'
          ;(form.val as (filter: string, data: Record<string, unknown>) => void)('form', {
            // `platforms: 0` 没有对应 radio ⇒ layui 的 `form.val` 会把所有平台单选取消选中
            // （对标 `addShop(0, 0, …)` 的新增初值，spec §2.4）。
            platforms: row ? row.platform : 0,
            // 新增与编辑都预填**平台** AppId（只读，租户改不了）。
            appId: platformAppId,
            // AppSecret 一次都不进浏览器：恒空 + 那一行隐藏。
            appSecret: '',
            // E-0253：授权码**没有回显可预填**（接口只给布尔）⇒ 新增与编辑都从空框开始；
            // 留空保存 = 不改（后端 PATCH 空串 = 不改），要换码就填新值。
            accessToken: '',
            // `''` = 首项「不设置」；`0` 不是合法来源 id（E-0021：别把空串与 0 当同一件事）。
            tbsoure: row?.order_source ?? '',
            remark: row ? row.remark : '',
            shopSellerNick: row ? row.shop_name : '',
          })
          ;(form.render as () => void)()
          layerIndex = openPanelLayer(
            el,
            layui,
            layer,
            bindingDialogOptions(row !== null, panel),
            PANEL_SELECTOR,
          )
        }

        /** 保存（对标 `$('.saveInterfaceBtn').on('click')`，`pddshipsetting.js:303-345`）。 */
        async function submit(): Promise<void> {
          const panel = findPanel(el, PANEL_SELECTOR)
          const validated = validateBindingForm({ platform: checkedPlatform(panel) })
          if (validated.error !== '') {
            msg(validated.error)
            return
          }
          // ⚠️ `order_source` 走 `resolveOrderSource` **判过才带**：字典没取到（或认不出已有绑定的来源）时
          //    下拉的 `''` 不代表用户想清空，照发会**静默清掉已有绑定**（数据丢失方向）。
          const orderSource = resolveOrderSource({
            selected: selectValue(panel, 'tbsoure'),
            sourcesLoaded,
            existing: editing?.order_source ?? null,
            knownSourceIds: new Set(sourceNames.keys()),
          })
          const payload: ShopBindingPayload = {
            platform: validated.platform,
            shop_name: input(panel, 'shopSellerNick')?.value ?? '',
            // 空串在后端 PATCH 里是「不改」（`ShopBindingSerializer.update()`）：只改备注不会清掉授权码。
            access_token: input(panel, 'accessToken')?.value ?? '',
            remark: input(panel, 'remark')?.value ?? '',
          }
          // `undefined` = 不带这个键（保留原值）；`null` = 用户主动选了「不设置」⇒ 显式清空。
          if (orderSource !== undefined) payload.order_source = orderSource
          try {
            // `editing` 是**当前行**（不用 `#ptid` 的字符串）——判据用「有没有这一行」，不用真值。
            if (editing) await updateShopBinding(editing.id, payload)
            else await createShopBinding(payload)
          } catch (e) {
            // 失败**原样**显示后端 `msg`（业务码由 `api/http.ts` 的统一分支映射），不换文案、不吞、不关窗。
            msg(errText(e))
            return
          }
          if (layerIndex !== null) {
            ;(layer.close as (i: number) => void)(layerIndex)
            layerIndex = null
          }
          editing = null
          void reload()
          msg('保存成功')
        }

        /** 删除（确认语与按钮逐字照 `pddshipsetting.js:304-305`）。 */
        function remove(row: ShopBindingRow): void {
          ;(layer.confirm as (t: string, o: unknown, cb: (i: number) => void) => void)(
            '是否确认删除这个店铺对接？',
            { btn: ['确认删除', '点错了'] },
            (index: number) => {
              ;(layer.close as (i: number) => void)(index)
              void deleteShopBinding(row.id).then(
                () => {
                  void reload()
                  msg('删除成功')
                },
                (e: unknown) => msg(errText(e)),
              )
            },
          )
        }

        /* ── 页内点击的统一收口（**捕获阶段**） ──
         *
         * ⚠️ **为什么挂 `document` 而不是 `root`**：`openPanelLayer` 会把 `.modifyParamDiv`
         * **搬进 `.layui-layer-content`**（挂在 `document.body` 下、`root` 之外）——弹层里的
         * 「获取授权」与「保存设置」就在那里面，`root` 上的委托收不到它们的点击。
         * island 页跑在 iframe 里（`document` 就是本页，收不到别的页面的点击），卸载时移除。
         *
         * ⚠️ **必须捕获阶段**：layui 的 `form` 模块在冒泡阶段给 `.layui-form-radio` 挂了处理，
         * 冒泡阶段拦已经晚了——单选会**真的切过去**（= 假成功，标准 §5 禁止的形状）。
         * 同一手法见 `artifactPage.ts:258-279` 与 `system-sourelist.ts:512-528`。
         */
        const onDocumentClick = (event: Event): void => {
          const target = event.target as HTMLElement | null
          if (!target?.closest) return
          // 1) 弹层里的**真**保存：对标是 `<a class="… saveInterfaceBtn">`（**不是** `lay-submit`）。
          if (target.closest('.saveInterfaceBtn')) {
            event.preventDefault()
            event.stopPropagation()
            void submit()
            return
          }
          // 2) 弹层里的「获取授权」→ 尚未开放（R4：自动授权下一轮做）。
          if (target.closest('.getAuth')) {
            event.preventDefault()
            event.stopPropagation()
            msg('「获取授权」尚未开放')
            return
          }
          // 3) 弹层里的其它控件（平台单选 / 授权码 / 来源下拉）是**真的**，不拦。
          if (target.closest(PANEL_SELECTOR)) return
          // 4) 页面里「对标有、后端没有」的控件：接口量链接 / 五段平台功能设置的单选 /
          //    「保存以上设置」（spec §7.3-P2、R4）。
          const el2 = target.closest<HTMLElement>(NOT_OPEN_SELECTOR)
          if (!el2) return
          event.preventDefault()
          event.stopPropagation()
          msg(`「${notOpenLabel(el2)}」尚未开放`)
        }
        document.addEventListener('click', onDocumentClick, true)

        /* ── 表格事件（layui 的 `table.on`） ── */
        ;(table.on as (e: string, cb: (obj: unknown) => void) => void)(
          `toolbar(${TABLE_ID})`,
          (obj: unknown) => {
            const event = (obj as { event?: string }).event
            if (event === 'refresh') void reload()
            else if (event === 'addBtn') openForm(null)
          },
        )
        ;(table.on as (e: string, cb: (obj: unknown) => void) => void)(
          `tool(${TABLE_ID})`,
          (obj: unknown) => {
            const typed = obj as { event?: string; data?: ShopBindingRow }
            const row = typed.data
            if (!row) return
            if (typed.event === 'submitEdit') openForm(row)
            else if (typed.event === 'submitDel') remove(row)
          },
        )

        void reload()

        return () => {
          document.removeEventListener('click', onDocumentClick, true)
          if (layerIndex !== null) (layer.close as (i: number) => void)(layerIndex)
        }
      },
    },
    ctx,
  )
}

export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountPddshipsettingPage(root, ctx)
}

export default { mount } satisfies IslandPage
