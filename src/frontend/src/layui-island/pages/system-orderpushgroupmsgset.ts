/**
 * 【自动发消息到微信群设置】页（island key = `system-orderpushgroupmsgset`，路由
 * `/system/wechat-group-push`；2026-10-17 从「证据件渲染」改成**真功能**）。
 *
 * 对标 `/System/OrderPushGroupMsgSet`（`OrderPushGroupMsgSet.js`）的可见结构照抄：
 * 顶部工具栏（刷新 / 新增一个设置）+ 单表 + 隐藏的新增/编辑弹层；列有「发送时机 / 限定游戏 /
 * 消息类型 / 呼叫全员 / 追加说明 / 启用状态（开关）/ 操作」。
 *
 * ## 我方有意新增 / 偏离（业务方 2026-10-17 原话：「不局限于企微机器人，只要是 webhookurl
 * 都可以，我们自己去适配；现在先用企微机器人和自定义渠道即可」）
 *
 * 1. **渠道类型**（`wecom` / `custom`）：对标只认企微群机器人；我们多一档自定义 Webhook，
 *    载荷约定见 `src/apps/business/order_push.py` 文件头的表；
 * 2. **规则名称**（可选）：多条规则摆在一起时靠它分辨；
 * 3. **没有「内部抢单群聊 ID」**：那是对标自家聊天系统的概念，本项目没有；
 * 4. **发送时机多一行提示**：`发交易平台时` 这一档今天**不会发**（交易平台 M6 未做），
 *    页面上必须说破；
 * 5. **操作列多一颗「测试」**：点一下当场发一条测试消息，通不通立刻知道（后端同步回结果）。
 *
 * ## 载荷（自定义渠道）
 *
 * `{event, order_no, status, from_status, game, project, price, msg_type, content,
 * text:{content}, markdown:{content}, timestamp}`。页面上的说明只讲「通用 JSON」，
 * 精确形状以后端模块为唯一出处。
 *
 * ## 四条 island 陷阱
 *
 * ① 类名与选择器分成两个常量；② 以 `(` 开头的语句前置分号；③ 先 `bootstrapIsland` →
 * `root.innerHTML` → `initIslandElements`；④ 页里没有定时器。
 */
import { fetchAllGames, type ConfigRow } from '@/api/config'
import {
  ORDER_PUSH_PAGE_SIZE,
  TIMING_LABELS,
  createOrderPushSetting,
  deleteOrderPushSetting,
  listOrderPushSettings,
  testOrderPushSetting,
  timingHint,
  updateOrderPushSetting,
  type OrderPushSetting,
  type PushChannel,
  type PushMsgType,
  type PushTiming,
} from '@/api/orderPush'
import { escapeHtml } from '@/layui-island/escape'
import { bootstrapIsland, initIslandElements, type IslandContext, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

export const PAGE_KEY = 'system-orderpushgroupmsgset'
export const TITLE = '自动发消息到微信群设置'
export const TABLE_ID = 'tableList'
export const FORM_ID = 'orderPushDialog'
export const NODE = 'system.order_push_group_settings'

export const TIMINGS: PushTiming[] = [0, 10, 20, 30]

/** 页面上的渠道文案（与后端 `Channel` 的 label 逐字一致）。 */
export const CHANNEL_LABELS: Record<PushChannel, string> = {
  wecom: '企业微信群机器人',
  custom: '自定义 Webhook',
}

export const MSG_TYPE_LABELS: Record<PushMsgType, string> = {
  markdown: 'Markdown',
  text: '文本',
}

/** 表格里展示 Webhook 时只显示 host + 省略号（URL 里带 key，等于凭据）。 */
export function webhookHost(url: string): string {
  const match = /^https?:\/\/([^/?#]+)/i.exec(url ?? '')
  return match ? `${match[1]}/…` : '—'
}

/** 规则里勾的游戏名（按全量游戏顺序；查不到 id 就跳过）。 */
export function gamesLabel(ids: readonly number[], games: readonly ConfigRow[]): string {
  const names = games.filter((g) => ids.includes(g.id)).map((g) => g.name)
  return names.length > 0 ? names.join('、') : '—'
}

/** 表单本地校验（后端还会再判一次；这里先让用户少跑一趟）。 */
export function validatePushForm(input: {
  webhookUrl: string
  games: readonly number[]
  timing: number | null
}): string | null {
  const url = (input.webhookUrl ?? '').trim()
  if (!url) return 'Webhook 地址不能为空'
  if (!/^https?:\/\/[^\s]+$/i.test(url)) return 'Webhook 地址必须是 http:// 或 https:// 开头的完整地址'
  if (input.games.length === 0) return '至少要选择一个游戏'
  if (input.timing === null || !TIMINGS.includes(input.timing as PushTiming)) {
    return '请选择发送时机'
  }
  return null
}

/** 从请求体的原始值里解析勾选的游戏 id（纯函数：给测试直接用）。 */
export function parseGameIds(values: readonly string[]): number[] {
  return values
    .map((v) => Number.parseInt(v, 10))
    .filter((id) => Number.isInteger(id) && id > 0)
}

/** 表格列（`canManage` 目前恒 true —— 页面路由本身就要这个节点；保留参数便于测试）。 */
export function buildCols(canManage: boolean): Array<Record<string, unknown>> {
  return [
    {
      field: 'name',
      title: '规则名称',
      width: 120,
      templet: (d: OrderPushSetting) => escapeHtml(d.name || '—'),
    },
    {
      field: 'timing',
      title: '发送时机',
      width: 200,
      align: 'center',
      templet: (d: OrderPushSetting) => {
        const color = d.timing === 10 ? '#ff6a00' : d.timing === 20 ? '#FF5722' : '#219227'
        return `<span style="color:${color}" title="发送时机">${escapeHtml(TIMING_LABELS[d.timing] ?? String(d.timing))}</span>`
      },
    },
    {
      field: 'channel',
      title: '渠道',
      width: 140,
      align: 'center',
      templet: (d: OrderPushSetting) =>
        `${escapeHtml(CHANNEL_LABELS[d.channel] ?? d.channel)}` +
        `<div style="color:#999" title="${escapeHtml(d.webhook_url)}">${escapeHtml(webhookHost(d.webhook_url))}</div>`,
    },
    {
      field: 'games',
      title: '限定游戏',
      minWidth: 150,
      align: 'left',
      templet: (d: OrderPushSetting) => escapeHtml(gamesLabel(d.games, ALL_GAMES)),
    },
    {
      field: 'msg_type',
      title: '消息类型',
      width: 90,
      align: 'center',
      templet: (d: OrderPushSetting) => escapeHtml(MSG_TYPE_LABELS[d.msg_type] ?? d.msg_type),
    },
    {
      field: 'call_all',
      title: '呼叫全员',
      width: 90,
      align: 'center',
      templet: (d: OrderPushSetting) => `<span>${d.call_all ? '是' : '否'}</span>`,
    },
    {
      field: 'remark',
      title: '追加说明',
      width: 120,
      align: 'left',
      templet: (d: OrderPushSetting) => escapeHtml(d.remark || ''),
    },
    {
      field: 'is_active',
      title: '<span lay-tips="绿色代表启用时">启用状态</span>',
      width: 105,
      align: 'center',
      templet: (d: OrderPushSetting) =>
        `<input type="checkbox" name="enabled" lay-skin="switch" lay-text="启用|停用"` +
        ` lay-filter="pushEnabled" value="${d.id}"${d.is_active ? ' checked' : ''} />`,
    },
    {
      title: '操作',
      width: 200,
      align: 'center',
      templet: (d: OrderPushSetting) =>
        canManage
          ? `<a class="layui-btn layui-btn-xs" data-act="test" data-id="${d.id}" title="发一条测试消息">测试</a>` +
            ` <a class="layui-btn layui-btn-xs layui-btn-warm" data-act="edit" data-id="${d.id}" title="编辑参数">` +
            '<i class="layui-icon layui-icon-edit"></i></a>' +
            ` <a class="layui-btn layui-btn-xs layui-btn-danger" data-act="del" data-id="${d.id}" title="删除设置">` +
            '<i class="layui-icon layui-icon-delete"></i></a>'
          : '',
    },
  ]
}

/**
 * 当前渲染用的游戏字典（表格列 / 弹窗共用）。
 *
 * ⚠️ 用模块级变量是**这一页的取舍**：`buildCols` 是纯函数（测试直接调），而 layui 的
 * `templet` 只拿得到行数据、拿不到页面闭包。页面每次装载都会**先灌再 render**，不会串页
 * （同一时刻只有一个实例）。
 */
let ALL_GAMES: ConfigRow[] = []

export function pageHtml(): string {
  return (
    '<style>.push-page{padding:0 12px 12px}.push-toolbar{padding:12px 0 0}' +
    '.push-toolbar .layui-form-item{margin-bottom:10px}' +
    '.push-games{max-height:130px;overflow:auto;border:1px solid #eee;padding:6px;border-radius:3px}' +
    '.push-games label{display:inline-block;min-width:130px;margin:2px 8px 2px 0}' +
    '.push-hint{color:#999;line-height:22px}.push-hint.warn{color:#FF5722}</style>' +
    '<div class="push-page layui-form">' +
    '<form class="layui-form push-toolbar" lay-filter="pushToolbar">' +
    '<div class="layui-btn-container">' +
    '<button type="button" class="layui-btn layui-btn-xs" data-act="refresh" title="刷新当前数据">' +
    '<i class="layui-icon">&#xe669;</i>刷新列表</button>' +
    '<button type="button" class="layui-btn layui-btn-xs layui-btn-danger" data-act="add" title="新增一个设置">' +
    '<i class="layui-icon layui-icon-add-1"></i>新增一个设置</button>' +
    '<span id="pushTotal" style="margin-left:8px;color:#ff6a00"></span>' +
    '</div></form>' +
    `<table id="${TABLE_ID}" lay-filter="${TABLE_ID}"></table>` +
    '</div>'
  )
}

function radio(name: string, value: string, label: string, checked: boolean, filter = ''): string {
  return (
    `<input type="radio" name="${name}" value="${value}" title="${escapeHtml(label)}"` +
    `${filter ? ` lay-filter="${filter}"` : ''}${checked ? ' checked' : ''} />`
  )
}

/** 新增 / 编辑弹窗的 HTML。 */
export function dialogHtml(row: OrderPushSetting | null, games: readonly ConfigRow[]): string {
  const channel = row?.channel ?? 'wecom'
  const timing = row?.timing ?? 0
  const msgType = row?.msg_type ?? 'markdown'
  const selectedGames = new Set(row?.games ?? [])
  const gameBoxes = games
    .map(
      (g) =>
        `<label><input type="checkbox" name="game" value="${g.id}"${selectedGames.has(g.id) ? ' checked' : ''} /> ` +
        `${escapeHtml(g.name)}</label>`,
    )
    .join('')
  return (
    `<div id="${FORM_ID}"><form class="layui-form" lay-filter="${FORM_ID}">` +
    '<div class="layui-form-item"><label class="layui-form-label">规则名称</label>' +
    `<div class="layui-input-block"><input class="layui-input" name="name" maxlength="50"` +
    ` placeholder="不填也行，多条规则时用来区分" value="${escapeHtml(row?.name ?? '')}" /></div></div>` +
    '<div class="layui-form-item"><label class="layui-form-label">渠道类型</label>' +
    '<div class="layui-input-block">' +
    radio('channel', 'wecom', '企业微信群机器人', channel === 'wecom', 'pushChannel') +
    radio('channel', 'custom', '自定义 Webhook', channel === 'custom', 'pushChannel') +
    '<div class="push-hint" data-role="channel-hint"></div></div></div>' +
    '<div class="layui-form-item"><label class="layui-form-label">Webhook 地址</label>' +
    '<div class="layui-input-block"><input class="layui-input" name="webhook_url" maxlength="500" ' +
    'placeholder="企微群机器人地址，或你方自己的 HTTP(S) 接收地址" ' +
    `value="${escapeHtml(row?.webhook_url ?? '')}" /></div></div>` +
    '<div class="layui-form-item"><label class="layui-form-label">限定游戏</label>' +
    `<div class="layui-input-block"><div class="push-games">${gameBoxes || '还没有游戏，请先到「游戏管理」新增'}</div></div></div>` +
    '<div class="layui-form-item"><label class="layui-form-label">发送时机</label>' +
    '<div class="layui-input-block">' +
    TIMINGS.map((t) => radio('timing', String(t), TIMING_LABELS[t], timing === t, 'pushTiming')).join('') +
    '<div class="push-hint" data-role="timing-hint"></div></div></div>' +
    '<div class="layui-form-item"><label class="layui-form-label">是否@全员</label>' +
    '<div class="layui-input-block">' +
    radio('call_all', '1', '需要', row?.call_all === true) +
    radio('call_all', '0', '不需要', row?.call_all !== true) +
    '<div class="push-hint">Markdown 模式下企微机器人不支持 @全员；要真提醒请选文本模式</div></div></div>' +
    '<div class="layui-form-item"><label class="layui-form-label">消息类型</label>' +
    '<div class="layui-input-block">' +
    radio('msg_type', 'markdown', 'Markdown 模式', msgType === 'markdown') +
    radio('msg_type', 'text', '文本模式', msgType === 'text') +
    '<div class="push-hint">自定义渠道两种都会收到，按你方适配器取用</div></div></div>' +
    '<div class="layui-form-item"><label class="layui-form-label">追加说明</label>' +
    `<div class="layui-input-block"><input class="layui-input" name="remark" maxlength="100"` +
    ` value="${escapeHtml(row?.remark ?? '')}" /></div></div>` +
    '</form></div>'
  )
}

/** 弹窗里那两句随选项变化的提示（纯文本，测试可直接钉）。 */
export function channelHint(channel: PushChannel): string {
  return channel === 'wecom'
    ? '企微群里「添加群机器人」后复制的 Webhook 地址；消息按企微原生 text / markdown 格式发送。'
    : '任意 HTTP(S) 地址。我们发通用 JSON（content + text/markdown 两套片段），由你方自行适配。'
}

export async function mountPushPage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  const layui = await bootstrapIsland(['form', 'table', 'layer', 'util'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule

  document.title = TITLE
  root.innerHTML = pageHtml()
  initIslandElements(layui as LayuiNamespace)

  const msg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: 2000 })
  }
  const el = <T extends HTMLElement>(selector: string): T | null =>
    root.querySelector<T>(selector)

  let rows: OrderPushSetting[] = []
  let dialogIndex: number | null = null

  const closeDialog = (): void => {
    if (dialogIndex !== null) {
      ;(layer.close as (i: number) => void)(dialogIndex)
      dialogIndex = null
    }
  }

  function renderTable(): void {
    ;(table.reloadData as (id: string, o: unknown) => void)(TABLE_ID, {
      data: rows,
      page: false,
    })
  }

  async function reload(): Promise<boolean> {
    try {
      const page = await listOrderPushSettings({ page: 1, page_size: ORDER_PUSH_PAGE_SIZE })
      rows = page.items
      renderTable()
      const total = el<HTMLElement>('#pushTotal')
      if (total) total.textContent = `共 ${page.total} 条`
      if (page.total > page.items.length) {
        msg(`规则太多，只显示了前 ${page.items.length} 条（共 ${page.total} 条）`)
      }
      ;(form.render as (type?: string) => void)('checkbox')
      return true
    } catch (e) {
      msg(e instanceof Error ? e.message : '加载失败')
      return false
    }
  }

  function readChecked(host: HTMLElement, name: string): string {
    return host.querySelector<HTMLInputElement>(`input[name="${name}"]:checked`)?.value ?? ''
  }

  function updateHints(host: HTMLElement): void {
    const channel = (readChecked(host, 'channel') || 'wecom') as PushChannel
    const channelHintEl = host.querySelector<HTMLElement>('[data-role="channel-hint"]')
    if (channelHintEl) channelHintEl.textContent = channelHint(channel)
    const timingRaw = readChecked(host, 'timing')
    const timingHintEl = host.querySelector<HTMLElement>('[data-role="timing-hint"]')
    if (timingHintEl) {
      const timing = (timingRaw === '' ? null : Number(timingRaw)) as PushTiming | null
      timingHintEl.textContent = timing === null ? '' : timingHint(timing)
      timingHintEl.classList.toggle('warn', timing === 20)
    }
  }

  function openDialog(row: OrderPushSetting | null): void {
    closeDialog()
    const index = (layer.open as (o: unknown) => number)({
      type: 1,
      title: row ? '编辑参数' : '新增一个设置',
      area: ['860px', 'auto'],
      maxHeight: 680,
      shade: [0.3, '#333'],
      shadeClose: true,
      skin: 'layui-layer-lan',
      anim: 8,
      content: `<div style="padding:12px 16px 2px">${dialogHtml(row, ALL_GAMES)}</div>`,
      btn: ['保存设置', '取消'],
      yes: (layerIndex: number) => {
        void submit(layerIndex, row)
      },
      end: () => {
        dialogIndex = null
      },
    })
    dialogIndex = index
    ;(form.render as (type?: string) => void)()
    const host = document.getElementById(FORM_ID)
    if (host) {
      updateHints(host)
      host.addEventListener('change', () => updateHints(host))
    }
  }

  async function submit(layerIndex: number, row: OrderPushSetting | null): Promise<void> {
    const host = document.getElementById(FORM_ID)
    if (!host) return
    const webhookUrl = (host.querySelector<HTMLInputElement>('[name="webhook_url"]')?.value ?? '').trim()
    const games = parseGameIds(
      Array.from(host.querySelectorAll<HTMLInputElement>('input[name="game"]:checked')).map(
        (box) => box.value,
      ),
    )
    const timingRaw = readChecked(host, 'timing')
    const timing = timingRaw === '' ? null : (Number(timingRaw) as PushTiming)
    const invalid = validatePushForm({ webhookUrl, games, timing })
    if (invalid !== null) {
      msg(invalid)
      return
    }
    const input = {
      name: (host.querySelector<HTMLInputElement>('[name="name"]')?.value ?? '').trim(),
      channel: (readChecked(host, 'channel') || 'wecom') as PushChannel,
      webhook_url: webhookUrl,
      games,
      timing: timing as PushTiming,
      msg_type: (readChecked(host, 'msg_type') || 'markdown') as PushMsgType,
      call_all: readChecked(host, 'call_all') === '1',
      remark: (host.querySelector<HTMLInputElement>('[name="remark"]')?.value ?? '').trim(),
      is_active: row?.is_active ?? true,
    }
    try {
      if (row) {
        await updateOrderPushSetting(row.id, input)
      } else {
        await createOrderPushSetting(input)
      }
      msg('保存成功')
      ;(layer.close as (i: number) => void)(layerIndex)
      dialogIndex = null
      await reload()
    } catch (e) {
      msg(e instanceof Error ? e.message : '保存失败')
    }
  }

  async function toggle(row: OrderPushSetting, checked: boolean): Promise<void> {
    try {
      await updateOrderPushSetting(row.id, { is_active: checked })
      row.is_active = checked
      msg(checked ? '已启用' : '已停用')
    } catch (e) {
      msg(e instanceof Error ? e.message : '保存失败')
      await reload()
    }
  }

  async function test(row: OrderPushSetting): Promise<void> {
    msg('正在发送测试消息…')
    try {
      await testOrderPushSetting(row.id)
      msg('测试消息已发送')
    } catch (e) {
      msg(e instanceof Error ? e.message : '发送失败')
    }
  }

  function remove(row: OrderPushSetting): void {
    ;(layer.confirm as (t: string, o: unknown, cb: (i: number) => void) => void)(
      `是否确认删除这个设置${row.name ? `「${escapeHtml(row.name)}」` : ''}？<br>删除后它不会再发任何消息。`,
      { title: '提示', btn: ['确认删除', '点错了'] },
      (index: number) => {
        ;(layer.close as (i: number) => void)(index)
        void (async () => {
          try {
            await deleteOrderPushSetting(row.id)
            msg('删除成功')
            await reload()
          } catch (e) {
            msg(e instanceof Error ? e.message : '删除失败')
          }
        })()
      },
    )
  }

  const onClick = (event: Event): void => {
    const target = event.target as HTMLElement
    const action = target.closest<HTMLElement>('[data-act]')
    if (!action) return
    const act = action.dataset.act
    const id = Number(action.dataset.id ?? 0)
    if (act === 'refresh') {
      event.preventDefault()
      void reload().then((ok) => {
        if (ok) msg('已刷新')
      })
      return
    }
    if (act === 'add') {
      event.preventDefault()
      openDialog(null)
      return
    }
    const row = rows.find((r) => r.id === id)
    if (!row) return
    if (act === 'edit') {
      event.preventDefault()
      openDialog(row)
      return
    }
    if (act === 'del') {
      event.preventDefault()
      remove(row)
      return
    }
    if (act === 'test') {
      event.preventDefault()
      void test(row)
    }
  }
  root.addEventListener('click', onClick)

  ;(form.on as (e: string, cb: (obj: { elem: HTMLInputElement; value: string }) => void) => void)(
    'switch(pushEnabled)',
    (obj) => {
      const row = rows.find((r) => String(r.id) === String(obj.elem.value))
      if (!row) return
      void toggle(row, obj.elem.checked)
    },
  )
  // 弹窗里那两句提示跟着单选走（layui 的单选点击**不一定**触发原生 change，所以同时挂
  // `form.on('radio(...)')` 与 open 时那条原生 change——判据是「换选项时提示一定变」）。
  const refreshDialogHints = (): void => {
    const host = document.getElementById(FORM_ID)
    if (host) updateHints(host)
  }
  ;(form.on as (e: string, cb: () => void) => void)('radio(pushChannel)', refreshDialogHints)
  ;(form.on as (e: string, cb: () => void) => void)('radio(pushTiming)', refreshDialogHints)

  ALL_GAMES = (await fetchAllGames().catch((): ConfigRow[] => [])) as ConfigRow[]
  ;(table.render as (o: unknown) => void)({
    elem: `#${TABLE_ID}`,
    data: [],
    page: false,
    defaultToolbar: ['filter'],
    toolbar: false,
    cols: [buildCols(true)],
    text: { none: '还没有配置过推送规则，点上方「新增一个设置」' },
  })
  await reload()

  return () => {
    root.removeEventListener('click', onClick)
    closeDialog()
    root.innerHTML = ''
  }
}

export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountPushPage(root, ctx)
}

export default { mount } satisfies IslandPage
