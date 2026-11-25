/**
 * 【绑定用于结算打款的支付宝管理列表】页的 **layui 复刻页**
 * （对标物 `/Emp/bindalipaysettauth`，**证据件渲染**）。
 *
 * 形状：**单表列表页 + 1 个隐藏表单弹层**（页头说明条 + 0 个筛选控件 + 工具栏 2 颗按钮 + 表格 13 列）。
 *
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/emp-bindalipaysettauth.html`
 *    （源 `.tools/recon/suqing1/html/Emp__BindAlipaySettAuth.html` 的 L52-111）
 *  - 列定义与 `table.render` 参数 → `.tools/recon/suqing1/assets/JS2019/Emp/BindAlipaySettAuth.js`
 *    L19-112（`elem:'#tableList'`、**没有** `cellMinWidth`、`toolbar:'#toolbarDemo'`、
 *    `defaultToolbar:['filter']`（本页只写一次）、**`page: false`（分页关闭）**、
 *    `limit: 50`、`limits:[10,20,30,40,50,100]`）
 *  - 工具栏模板 → 同上 HTML 的 `#toolbarDemo`（L99-104）；行内按钮 → `#baroperating`（L105-108）
 *
 * ⚠️ **`page: false`**（JS L106）：分页**关闭**。`ArtifactTable.page` 传 `false`
 * （原样照抄），并**照样带上 `limit`/`pageLimits`**——它们在分页关闭时不渲染，但值来自本页 JS，
 * 留着与对标声明一致（同 `system-roleauthority` 的做法）。
 * ⚠️ 判据提醒：**不能用渲染出来的 `.layui-laypage` 判本页 `page` 真假**——我们表恒空（0 行），
 * 0 行的表无论 `page` 真假都不渲染分页栏（layui `0==n` 分支）。结论取自**读码**（JS L106）。
 *
 * ## 4 个弹窗（**只做外观，点了提示「尚未开放」**）
 *
 * 对标本页有 4 处弹层，全在 JS 里、没有后端我们一律不打开（`artifactPage` 的捕获阶段收口统一提示）：
 *  1. `addAuth()`（L142-154）`type:1` / `560x460` / `skin:'layui-layer-lan'` / 标题
 *     「新增一个用于结算的付款支付宝」，内容就是页内那个 `display:none` 的 `.newBindDiv`
 *     （证据件里逐字保留，这是本页唯一**可见标记**形态的弹窗）；
 *  2. `alipayAuth()`（L347-367）`type:2` / `400x440` / 标题「请完成支付宝功能授权」，
 *     内容是**外站** `toHost + '/pay/AlipayUniApplyAuth?bizId=…'`；
 *  3. `reAuth()`（L211-233）`type:1` / `area:'300px;'` / id `LAY_layuipro` / 按钮「签约完成」，
 *     标题「请完成支付宝签约」，同样指向外站 `/pay/AlipyAuth?bizId=…`；
 *  4. `form.on('submit(savebtn)')` 里 channel 非 0/1 那支（L296-324）标题「请完成支付宝服务签约」，
 *     按钮 `['签约完成','重新签约']`。
 *  后 3 个都是 `type:2`/外站 iframe 的**第三方签约页**（`https://www.jidanpu.com` /
 *  `https://trust.lingyuefangzhou.com`）⇒ 我们**不打开任何外站**，也不需要把它们做成标记
 *  （对标也不在 HTML 里，是运行期生成的），只在报告里点名。
 *
 * ## PII
 *
 * 本页是支付宝授权页：**账号/实名在证据件里没有值**——对标模板的 3 个账号输入框 `value` 原文
 * 就是空串（HTML L62/68/74），支付宝账号从不服务端渲染进标记。`lay-tips` 里那处
 * 「XXX县XXX网络科技（个体工商户）」是对标自带的**脱敏示例**（原文即 XXX），逐字保留。
 * 清空清单见本轮报告：**本页无需清空**（证据件已核，无 2088… 之类账号串）。
 *
 * ⚠️ 工具栏 2 颗按钮在对标里**没有图标**（HTML L101-102），逐字保留文案与 class。
 * ⚠️ 对标 `<title>` 是服务端渲染的「绑定用于结算打款的支付宝管理列表」（`Emp__BindAlipaySettAuth.html` L5）。
 */
import { fetchArtifactHtml } from '@/layui/artifactPage'
import { bootstrapIsland, initIslandElements, islandMsg } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'
import { alipayReviewMessage, alipayStatusLabel, authorizeAlipay, clearConfirmedAlipayRequest, createAlipayAuthorization, listAlipayAuthorizations, newAlipayRequestId, pendingAlipayRequestId, rememberAlipayRequestId, refreshAlipayAuthorization, identityAlipayAuthorization, refreshAlipayIdentity, revokeAlipayAuthorization, needsAlipayIdentity, type AlipayAuthorization } from '@/api/alipayAuthorization'
import type { ArtifactCol, ArtifactTable } from '@/layui/artifactPage'
import type { IslandContext, IslandPage } from '@/layui/island'

/** 证据件（页内 `<style>` + body 标记，零转义）。 */
const ARTIFACT_URL = 'replica/emp-bindalipaysettauth.html'
/** 对标 `page.limits`（`BindAlipaySettAuth.js` L108：`limits: [10, 20, 30, 40, 50, 100]`）。 */
export const PAGE_LIMITS = [10, 20, 30, 40, 50, 100]

/**
 * 表头（逐条照抄 `BindAlipaySettAuth.js` L24-105 的 `cols[0]`，共 **13 列**、**无勾选列**）。
 *
 * `sort:false` / `unresize:false` 是 layui 默认值，不写；`templet` 全是函数（L31/43/62/90）⇒ 留空白模板。
 * 注意 **`str6`（备注说明）带 `edit: true`**（L81）——单元格可编辑，别漏（漏了是静默差异，
 * 空表下 `parity` 看不见）。
 * ⚠️ 操作列 `width: 210`、`toolbar:'#baroperating'`（L104）；少了 `toolbar` 这列会静默变空列。
 */
export const COLS: ArtifactCol[] = [
  { field: 'str1', title: '付款支付宝账号', width: 260, align: 'center' },
  { field: 'str8', title: '支付宝名称', width: 120, align: 'center' },
  { field: 'id8', title: '通道', width: 80, align: 'center' },
  { field: 'str2', title: '服务签约状态', width: 120, align: 'center', unresize: true },
  { field: 'str13', title: '功能授权状态', width: 120, align: 'center', unresize: true },
  { field: 'str3', title: '申请时间', width: 150, align: 'center', unresize: true },
  { field: 'str4', title: '授权时间', width: 150, align: 'center' },
  { field: 'str6', title: '备注说明', width: 150, edit: true },
  { field: 'str10', title: '解除授权时间', width: 150, align: 'center' },
  { field: 'str11', title: '解除授权说明', width: 150, align: 'left' },
  { field: 'none', title: '系统提示', width: 220 },
  { field: 'str7', title: '操作人', width: 130, align: 'center' },
  // `fixed: ''`（= 不固定）是 layui 默认，不写。
  { title: '操作', width: 210, toolbar: '#baroperating' },
]

/** 表的声明（`elem` / `toolbar` / `page` / `defaultToolbar` 与对标同值）。 */
export const TABLE: ArtifactTable = {
  elem: '#tableList',
  toolbar: '#toolbarDemo',
  // 对标 L23 只写了一次 `defaultToolbar: ['filter']`（本页没有重复声明）。
  defaultToolbar: ['filter'],
  // 对标 L106：`page: false` —— **分页关闭**（不是"没写"，是显式 false）。
  page: false,
  // 分页关着，但因为本页 JS 确实声明了它们，原样带上备查（同 `system-roleauthority` 的做法）。
  limit: 50,
  pageLimits: PAGE_LIMITS,
  // 对标**没有** `cellMinWidth` ⇒ 不传。
  cols: COLS,
}

export default {
  mount: async (root: HTMLElement, _ctx: IslandContext) => {
    document.title = '绑定用于结算打款的支付宝管理列表'
    const layui: LayuiNamespace = await bootstrapIsland(['form', 'table', 'layer'])
    const table = layui.table as LayuiModule
    const layer = layui.layer as LayuiModule
    const form = layui.form as LayuiModule
    const jq = (layui as unknown as { $: (element: HTMLElement) => unknown }).$
    root.innerHTML = await fetchArtifactHtml(ARTIFACT_URL)
    root.querySelector('form')?.setAttribute('onsubmit', 'return false')
    const rowTemplate = root.querySelector('#baroperating')
    if (rowTemplate) rowTemplate.textContent = '{{# if (d.requires_platform_review) { }}<button type="button" class="layui-btn layui-btn-xs layui-btn-disabled" disabled>继续授权</button>{{# } else if (d.status === "signed" && d.next_action === "identity") { }}<a class="layui-btn layui-btn-xs layui-btn-normal" lay-event="identity">获取付款方身份</a>{{# } else if (["pending", "failed", "revoked"].includes(d.status)) { }}<a class="layui-btn layui-btn-xs layui-btn-normal" lay-event="authorize">{{ d.status === "revoked" ? "重新授权" : "继续授权" }}</a>{{# } }}<a class="layui-btn layui-btn-xs" lay-event="refreshAuth">刷新授权结果</a>{{# if (!d.requires_platform_review && ["signed", "authorized"].includes(d.status)) { }}<a class="layui-btn layui-btn-xs layui-btn-danger" lay-event="revoke">取消授权</a>{{# } }}'
    initIslandElements(layui)
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)
    const showError = (error: unknown): void => {
      const el = document.createElement('span')
      el.textContent = error instanceof Error ? error.message : '请求失败，请稍后重试'
      msg(el.innerHTML)
    }
    let busy = false
    let activeLayer: number | null = null
    let retryTimer: ReturnType<typeof setTimeout> | undefined
    let retryGeneration = 0
    let disposed = false
    function stopRetry(): void {
      retryGeneration++
      if (retryTimer !== undefined) clearTimeout(retryTimer)
      retryTimer = undefined
    }
    let rows: AlipayAuthorization[] = []
    const pendingNotice = document.createElement('p')
    pendingNotice.setAttribute('role', 'status')
    pendingNotice.textContent = '上次申请结果尚未确认。请刷新列表；如仍无记录，请填写原来的支付宝账号和实名重试。'
    pendingNotice.hidden = pendingAlipayRequestId() === null
    root.prepend(pendingNotice)
    const tableRef = (table.render as (options: unknown) => { reload(options: unknown): void })({
      elem: '#tableList', toolbar: '#toolbarDemo', defaultToolbar: ['filter'], page: false,
      cols: [COLS.map((col) => col.field === 'str6' ? { ...col, edit: undefined } : col)],
      data: [], text: { none: '暂无数据' }, done: () => (form.render as () => void)(),
    })
    function refreshTable(): void {
      tableRef.reload({ data: rows.map((row) => ({
        id: row.id, str1: row.account_masked, str8: row.real_name, id8: '支付宝有密转账',
        status: row.status, next_action: row.next_action, requires_platform_review: row.requires_platform_review === true,
        str2: alipayStatusLabel(row.status, row.last_error, row.requires_platform_review), str13: alipayStatusLabel(row.status, row.last_error, row.requires_platform_review),
        str3: row.created_at ?? '', str4: row.authorized_at ?? '', str6: '', str10: '', str11: '',
        none: alipayReviewMessage(row), str7: '',
      })) })
    }
    async function load(): Promise<void> {
      try {
        rows = await listAlipayAuthorizations()
        clearConfirmedAlipayRequest(rows)
        pendingNotice.hidden = pendingAlipayRequestId() === null
        refreshTable()
      }
      catch (error) { showError(error) }
    }
    function closeActive(): void {
      stopRetry()
      if (activeLayer !== null) (layer.close as (id: number) => void)(activeLayer)
      activeLayer = null
    }
    function openQr(row: AlipayAuthorization): void {
      if (disposed) return
      closeActive()
      const content = document.createElement('div')
      content.style.padding = '20px'
      const status = document.createElement('p'); status.textContent = alipayStatusLabel(row.status, row.last_error, row.requires_platform_review); content.append(status)
      let retryInfo: HTMLParagraphElement | undefined
      if (needsAlipayIdentity(row)) { const hint = document.createElement('p'); hint.textContent = '请使用付款支付宝扫码，授权获取付款方身份。'; content.append(hint) }
      if (row.qr_png_base64 && !row.requires_platform_review) {
        const img = document.createElement('img'); img.alt = '支付宝授权二维码'; img.src = `data:image/png;base64,${row.qr_png_base64}`; content.append(img)
      } else if ((!['authorized', 'signed'].includes(row.status) || needsAlipayIdentity(row)) && !row.requires_platform_review) {
        retryInfo = document.createElement('p'); retryInfo.textContent = row.retryable_login ? '正在自动尝试获取授权二维码，请稍候。' : '暂无授权二维码，请点击继续授权重试'; content.append(retryInfo)
      }
      const error = document.createElement('p'); error.textContent = alipayReviewMessage(row); content.append(error)
      const refresh = document.createElement('button'); refresh.type = 'button'; refresh.className = 'layui-btn'; refresh.textContent = '刷新授权结果'; content.append(refresh)
      refresh.addEventListener('click', async () => {
        if (busy) return
        stopRetry()
        if (retryInfo && row.retryable_login) retryInfo.textContent = '自动尝试已暂停，请关闭弹窗并刷新列表查看状态。'
        busy = true
        const generation = retryGeneration
        try {
          const wasIdentity = needsAlipayIdentity(row)
          let updated = await (wasIdentity ? refreshAlipayIdentity(row.id) : refreshAlipayAuthorization(row.id))
          if (generation !== retryGeneration || activeLayer === null) return
          if (!wasIdentity && needsAlipayIdentity(updated) && !updated.requires_platform_review && !updated.last_error) updated = await identityAlipayAuthorization(row.id)
          if (generation !== retryGeneration || activeLayer === null) return
          if (wasIdentity && needsAlipayIdentity(updated) && !updated.qr_png_base64 && !updated.requires_platform_review) updated = { ...updated, qr_png_base64: row.qr_png_base64 }
          await load(); if (generation === retryGeneration && activeLayer !== null) openQr(updated)
        }
        catch (error) { showError(error) }
        finally { busy = false }
      })
      const layerId = (layer.open as (options: unknown) => number)({ type: 1, title: '请完成支付宝授权', area: ['400px', '440px'], content: jq(content), beforeEnd: () => { if (activeLayer === layerId) { stopRetry(); activeLayer = null } } })
      activeLayer = layerId
      if (row.retryable_login && !row.requires_platform_review && (row.status === 'pending' || needsAlipayIdentity(row)) && !row.qr_png_base64) {
        const generation = retryGeneration
        retryTimer = setTimeout(async () => {
          retryTimer = undefined
          if (generation !== retryGeneration || activeLayer === null) return
          if (busy) {
            stopRetry()
            if (retryInfo) retryInfo.textContent = '自动尝试已暂停，请关闭弹窗并刷新列表查看状态。'
            return
          }
          busy = true
          try {
            const updated = await (needsAlipayIdentity(row) ? identityAlipayAuthorization(row.id) : authorizeAlipay(row.id))
            if (generation !== retryGeneration || activeLayer === null) return
            await load()
            if (generation === retryGeneration && activeLayer !== null) openQr(updated)
          } catch (error) {
            if (generation === retryGeneration) {
              stopRetry()
              if (retryInfo) retryInfo.textContent = '自动尝试已暂停，请关闭弹窗并刷新列表查看状态。'
              showError(error)
            }
          }
          finally { busy = false }
        }, 3000)
      }
    }
    async function startAuthorization(row: AlipayAuthorization): Promise<void> {
      if (busy || row.requires_platform_review) return
      stopRetry()
      busy = true
      try { const updated = await authorizeAlipay(row.id); await load(); openQr(updated) }
      catch (error) { showError(error) }
      finally { busy = false }
    }
    async function startIdentity(row: AlipayAuthorization): Promise<void> {
      if (busy || row.requires_platform_review) return
      stopRetry(); busy = true
      try { const updated = await identityAlipayAuthorization(row.id); await load(); openQr(updated) }
      catch (error) { showError(error) }
      finally { busy = false }
    }
    function confirmRevoke(row: AlipayAuthorization): void {
      if (busy || row.requires_platform_review) return
      ;(layer.confirm as (text: string, options: unknown, callback: (id: number) => void) => void)(
        '确定取消该付款支付宝的授权吗？取消后不能用于打款，可重新授权。',
        { title: '确认取消授权', btn: ['确认取消授权', '返回'] },
        (id) => {
          if (busy) return
          ;(layer.close as (id: number) => void)(id)
          busy = true; stopRetry()
          void (async () => {
            try { await revokeAlipayAuthorization(row.id); closeActive(); await load() }
            catch (error) { await load(); showError(error) }
            finally { busy = false }
          })()
        },
      )
    }
    async function refreshAuthorization(row: AlipayAuthorization): Promise<void> {
      if (busy) return
      busy = true
      try { await (needsAlipayIdentity(row) ? refreshAlipayIdentity(row.id) : refreshAlipayAuthorization(row.id)); await load() }
      catch (error) { showError(error) }
      finally { busy = false }
    }
    function openAdd(): void {
      const source = root.querySelector<HTMLElement>('.newBindDiv')
      if (!source) return
      const content = source.cloneNode(true) as HTMLElement
      content.style.display = 'block'
      content.querySelector('#reNewPayAlipayAcc')?.closest('.layui-form-item')?.remove()
      content.querySelector('input[name="channel"]')?.closest('.layui-form-item')?.remove()
      content.querySelector('#remark')?.closest('.layui-form-item')?.remove()
      const account = content.querySelector<HTMLInputElement>('#newPayAlipayAcc')
      const name = content.querySelector<HTMLInputElement>('#newPayAlipayName')
      const submit = content.querySelector<HTMLButtonElement>('[lay-filter="savebtn"]')
      const pending = pendingAlipayRequestId()
      const requestId = pending ?? newAlipayRequestId()
      if (pending) {
        const notice = document.createElement('p')
        notice.textContent = '请使用上次提交的支付宝账号和实名重试，避免重复创建。'
        content.prepend(notice)
      }
      submit?.addEventListener('click', async (event) => {
        event.preventDefault()
        if (busy) return
        if (!account?.value.trim() || !name?.value.trim()) { msg('请填写支付宝账号和实名'); return }
        busy = true
        try {
          rememberAlipayRequestId(requestId)
          pendingNotice.hidden = false
          const row = await createAlipayAuthorization({ account: account.value.trim(), real_name: name.value.trim(), request_id: requestId })
          closeActive(); await load(); openQr(row)
        } catch (error) { showError(error) }
        finally { busy = false }
      })
      const layerId = (layer.open as (options: unknown) => number)({ type: 1, title: '新增一个用于结算的付款支付宝', area: ['560px', '460px'], content: jq(content), end: () => { if (activeLayer === layerId) activeLayer = null } })
      activeLayer = layerId
    }
    ;(table.on as (name: string, cb: (obj: { event: string; data: { id: number } }) => void) => void)('toolbar(tableList)', (obj) => {
      if (obj.event === 'refresh') void load()
      if (obj.event === 'addBookBtn') openAdd()
    })
    ;(table.on as (name: string, cb: (obj: { event: string; data: { id: number } }) => void) => void)('tool(tableList)', (obj) => {
      const row = rows.find((item) => item.id === obj.data.id)
      if (!row) return
      if (obj.event === 'authorize' && ['pending', 'failed', 'revoked'].includes(row.status) && !row.requires_platform_review) void startAuthorization(row)
      if (obj.event === 'identity' && needsAlipayIdentity(row) && !row.requires_platform_review) void startIdentity(row)
      if (obj.event === 'revoke' && ['signed', 'authorized'].includes(row.status) && !row.requires_platform_review) confirmRevoke(row)
      if (obj.event === 'refreshAuth') void refreshAuthorization(row)
    })
    await load()
    return () => { disposed = true; closeActive(); root.innerHTML = '' }
  },
} satisfies IslandPage
