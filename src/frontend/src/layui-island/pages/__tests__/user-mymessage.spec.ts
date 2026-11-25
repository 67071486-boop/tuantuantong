// @vitest-environment jsdom
import fs from 'node:fs'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Notification } from '@/api/notifications'
import {
  CATEGORY_OF_KIND,
  READ_COLS,
  TABLE_SPECS,
  UNREAD_COLS,
  activeTabOf,
  buildReadCols,
  buildUnreadCols,
  categoryOf,
  formatTime,
  isUnread,
  matchesTab,
  mountMyMessage,
  payloadText,
  toRow,
  unreadRowsFor,
  updateBadges,
  type MyMessageRow,
} from '../user-mymessage'

/**
 * 【我的消息】页的**接线测试**（本轮 = 把已复刻页接上后端）。
 *
 * 覆盖两件事，且都打在**真实出口**上：
 *  1. 请求形状：列表发 `GET /api/business/notifications/`、标记已读发
 *     `POST /api/business/notifications/{id}/read/`（单条；后端没有批量端点）；
 *  2. 渲染出口：`table.render` 拿到的 `data` 就是「当前侧边页签 + 未读」过滤后的行。
 *
 * 端点事实见 `apps/business/api/notification_views.py:35-63` 与 `urls.py:55-64`；
 * 对标页的 templet / 徽标 / `layer.open` 参数见
 * `.tools/recon/suqing1/assets/JS2019/User/mymessage.js`（行号在页面文件头）。
 */

const h = vi.hoisted(() => ({
  renders: [] as Array<Record<string, unknown>>,
  handlers: new Map<string, (obj: { event?: string }) => void>(),
  checked: [] as MyMessageRow[],
  msgs: [] as string[],
  opened: [] as Array<Record<string, unknown>>,
}))

vi.mock('@/api/http', () => ({ request: vi.fn() }))
vi.mock('@/layui/island', () => ({
  bootstrapIsland: async () => ({
    form: { render: () => undefined },
    table: {
      render: (o: unknown) => {
        h.renders.push(o as Record<string, unknown>)
      },
      on: (event: string, cb: (obj: { event?: string }) => void) => {
        h.handlers.set(event, cb)
      },
      checkStatus: () => ({ data: h.checked }),
      reload: () => undefined,
    },
    layer: {
      msg: (text: string) => {
        h.msgs.push(text)
      },
      open: (o: unknown) => {
        h.opened.push(o as Record<string, unknown>)
      },
    },
  }),
  initIslandElements: () => undefined,
  islandMsg: (layer: { msg: (t: string) => void }, text: string) => layer.msg(text),
}))

import { request } from '@/api/http'
import { BizError } from '@/api/errors'

const mockedRequest = vi.mocked(request)
const REPLICA = path.resolve(__dirname, '../../../../public/replica/user-mymessage.html')
const artifact = fs.readFileSync(REPLICA, 'utf8')

type Call = { method?: string; url?: string; params?: unknown }
const calls: Call[] = []

function notification(
  id: number,
  kind: string,
  readAt: string | null = null,
): Notification {
  const orderNo = `1234567890123456${String(id).padStart(4, '0')}`
  return {
    id,
    kind,
    order_no: orderNo,
    source_order_no: null,
    payload: { order_no: orderNo, from_status: '进行中' },
    read_at: readAt,
    created_at: '2026-09-21T08:00:00+08:00',
  }
}

/** 4 条：待审核未读 / 被抢走未读 / 被指派已读 / 被指派未读 —— 三档各有未读，便于验徽标。 */
const FIXTURES: Notification[] = [
  notification(1, '待审核'),
  notification(2, '被抢走'),
  notification(3, '被指派', '2026-09-20T10:00:00+08:00'),
  notification(4, '被指派'),
]

function respondWith(list: Notification[]): void {
  mockedRequest.mockImplementation((async (config: Call) => {
    calls.push(config)
    return { items: list, total: list.length, page: 1, page_size: 200 }
  }) as never)
}

/** 最近一次**未读表**的 render 选项（每次 refresh 会 push 未读表 + 已读表两条）。 */
const lastUnreadRender = (): Record<string, unknown> => h.renders[h.renders.length - 2]!
const lastReadRender = (): Record<string, unknown> => h.renders[h.renders.length - 1]!

/**
 * 每个 mount 都登记到 `mounted`：`afterEach` 无条件卸载。
 *
 * 为什么必须这样：`onTabClick` 挂在 **document** 上——某条用例在 `unmount()` 之前就断言失败时，
 * 监听器会留到**下一条用例**，于是下一次点击会触发两次 refresh（实测：本文件第一版就是这样
 * 让「页签切换」用例数出 6 次 render 而不是 4 次）。
 */
let mounted: (() => void) | null = null

async function mount(): Promise<{ root: HTMLElement; unmount: () => void }> {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, text: async () => artifact })),
  )
  const root = document.createElement('div')
  document.body.appendChild(root)
  const unmount = await mountMyMessage(root)
  mounted = unmount
  return { root, unmount }
}

afterEach(() => {
  mounted?.()
  mounted = null
})

beforeEach(() => {
  calls.length = 0
  h.renders.length = 0
  h.handlers.clear()
  h.checked.length = 0
  h.msgs.length = 0
  h.opened.length = 0
  document.body.innerHTML = ''
  mockedRequest.mockReset()
  respondWith(FIXTURES)
})

describe('分类映射（⚠️ 推断，见页面文件头）', () => {
  it('三类 kind 各归一档；未知 kind 不猜（返回 null）', () => {
    expect(CATEGORY_OF_KIND['待审核']).toBe('urgent')
    expect(CATEGORY_OF_KIND['被抢走']).toBe('important')
    expect(CATEGORY_OF_KIND['被指派']).toBe('general')
    expect(categoryOf('未来新加的第四类')).toBeNull()
  })

  it('未知 kind 在**三个页签里都可见**（fail-open：映射表没跟上也不会把消息藏没）', () => {
    expect(matchesTab('未来新加的第四类', 'urgent')).toBe(true)
    expect(matchesTab('未来新加的第四类', 'important')).toBe(true)
    expect(matchesTab('未来新加的第四类', 'general')).toBe(true)
    expect(matchesTab('被指派', 'general')).toBe(true)
    expect(matchesTab('被指派', 'urgent')).toBe(false)
  })
})

describe('行数据：后端字段 → 对标列', () => {
  it('未读判据是 `read_at === null`（不是真值）', () => {
    expect(isUnread({ read_at: null })).toBe(true)
    expect(isUnread({ read_at: '2026-09-20T10:00:00+08:00' })).toBe(false)
  })

  it('标题=`kind` 原文、内容=payload 渲染、发送人恒空、已读取 read_at', () => {
    const row = toRow(notification(7, '待审核'))
    expect(row).toMatchObject({
      id: 7,
      kind: '待审核',
      category: 'urgent',
      title: '待审核',
      content: '订单号：12345678901234560007；来源状态：进行中',
      sendUserName: '',
      isRead: 'N',
    })
    expect(toRow(notification(8, '被指派', '2026-09-20T10:00:00+08:00')).isRead).toBe('Y')
  })

  it('payload 两个字段都没有时内容为空串（不编文案）', () => {
    expect(payloadText({}, null)).toBe('')
    expect(payloadText({}, '12345678901234560009')).toBe('订单号：12345678901234560009')
    // ⭐ D-0301：来源号优先——payload 键在 ⇒ 用它；行上实时值兜底；都空 ⇒ 内置号。
    expect(
      payloadText({ order_no: '12345678901234560009', source_order_no: 'SRC-TEST-1' }, null),
    ).toBe('订单号：SRC-TEST-1')
    expect(payloadText({}, '12345678901234560009', 'SRC-TEST-1')).toBe('订单号：SRC-TEST-1')
    expect(
      payloadText({ order_no: '12345678901234560009', source_order_no: '' }, null, ''),
    ).toBe('订单号：12345678901234560009')
  })

  it('时间格式化成 `YYYY-MM-DD HH:mm:ss`；解析不了就原样返回', () => {
    // 用**本地**时区构造的瞬间，回读的本地墙上时间必须逐字相等。
    const local = new Date(2026, 8, 21, 8, 5, 3)
    expect(formatTime(local.toISOString())).toBe('2026-09-21 08:05:03')
    expect(formatTime('不是时间')).toBe('不是时间')
  })
})

describe('列：结构列不动，运行期列逐格挂 templet', () => {
  it('运行期列的文案/宽度与结构列逐条一致（没有因为接线改了表头）', () => {
    const runtime = buildUnreadCols()
    expect(runtime.map((c) => c.title)).toEqual(UNREAD_COLS.map((c) => c.title))
    expect(runtime.map((c) => c.width)).toEqual(UNREAD_COLS.map((c) => c.width))
    expect(runtime.map((c) => c.field)).toEqual(UNREAD_COLS.map((c) => c.field))
    expect(buildReadCols().map((c) => c.title)).toEqual(READ_COLS.map((c) => c.title))
  })

  it('templet 挂在**对的列**上：勾选列没有，2 列是档位，5 列是已读态', () => {
    const runtime = buildUnreadCols()
    const templets = runtime.map((c) => c.templet)
    expect(templets[0]).toBeUndefined() // 勾选列
    expect(typeof templets[1]).toBe('function') // 消息标题
    expect(typeof templets[2]).toBe('function') // 消息分类
    expect(typeof templets[3]).toBe('function') // 消息内容
    expect(typeof templets[4]).toBe('function') // 发送人
    expect(typeof templets[5]).toBe('function') // 是否已读
    expect(typeof templets[6]).toBe('function') // 接收时间
    expect(runtime[2]!.field).toBe('title') // 对标原文重复 field 的照抄，不许改
  })

  it('标题/内容渲染成对标那种 `viewMsg` 链接（带 data-id/title/content 与未读色 #666）', () => {
    const row = toRow(notification(5, '待审核'))
    const html = buildUnreadCols()[1]!.templet!(row)
    expect(html).toContain('class="viewMsg"')
    expect(html).toContain('style="color:#666"')
    expect(html).toContain('data-id="5"')
    expect(html).toContain('data-title="待审核"')
    expect(html).toContain('data-content="订单号：')
    // 已读行用对标 `#bfbfbf`
    expect(buildReadCols()[0]!.templet!(toRow(notification(6, '被指派', '2026-09-20T10:00:00+08:00')))).toContain(
      'color:#bfbfbf',
    )
  })

  it('分类列用对标三档配色；未知 kind 如实显示原文', () => {
    expect(buildUnreadCols()[2]!.templet!(toRow(notification(1, '待审核')))).toContain('color:#E03636')
    expect(buildUnreadCols()[2]!.templet!(toRow(notification(2, '被抢走')))).toContain('color:#C71585')
    expect(buildUnreadCols()[2]!.templet!(toRow(notification(4, '被指派')))).toContain('color:#DB9019')
    const unknown = buildUnreadCols()[2]!.templet!(toRow(notification(9, '新类型')))
    expect(unknown).toContain('新类型')
  })

  it('是否已读列：未读绿 #5FB878、已读橙 #FF5722（对标原值）', () => {
    expect(buildUnreadCols()[5]!.templet!(toRow(notification(1, '待审核')))).toContain('color:#5FB878')
    expect(
      buildUnreadCols()[5]!.templet!(toRow(notification(1, '待审核', '2026-09-20T10:00:00+08:00'))),
    ).toContain('color:#FF5722')
  })

  it('发送人列恒空（后端没有 sender 字段），时间是 `发自：` 链接', () => {
    const row = toRow(notification(1, '待审核'))
    expect(buildUnreadCols()[4]!.templet!(row)).toBe('')
    expect(buildUnreadCols()[6]!.templet!(row)).toContain('title="发自：2026-09-21 08:00:00"')
  })
})

describe('徽标与页签选择（纯函数）', () => {
  const rows = (): MyMessageRow[] => FIXTURES.map(toRow)

  it('三个徽标按**未读数**显隐（>0 显示数字，0 隐藏）——对标 done 回调口径', () => {
    const root = document.createElement('div')
    root.innerHTML =
      '<span class="news20"></span><span class="news10"></span><span class="news0"></span>'
    updateBadges(root, rows())
    expect([...root.querySelectorAll('span')].map((el) => el.textContent)).toEqual(['1', '1', '1'])
    expect(root.querySelector<HTMLElement>('.news20')!.style.display).toBe('')
    // 只剩已读 ⇒ 全部隐藏、文案清空。
    updateBadges(root, [toRow(notification(3, '被指派', '2026-09-20T10:00:00+08:00'))])
    expect([...root.querySelectorAll('span')].map((el) => el.textContent)).toEqual(['', '', ''])
    expect(root.querySelector<HTMLElement>('.news20')!.style.display).toBe('none')
  })

  it('`activeTabOf` 读 `layui-this`，读不到按对标默认的「急需处理」', () => {
    const root = document.createElement('div')
    root.innerHTML =
      '<div class="layui-tab sidetab"><ul class="layui-tab-title sidetitle">' +
      '<li lay-id="20" class="layui-this">急需处理</li><li lay-id="0">一般消息</li></ul></div>'
    expect(activeTabOf(root)).toBe('urgent')
    root.querySelector('.layui-tab-title li')!.classList.remove('layui-this')
    root.querySelector('[lay-id="0"]')!.classList.add('layui-this')
    expect(activeTabOf(root)).toBe('general')
    expect(activeTabOf(document.createElement('div'))).toBe('urgent')
  })

  it('未读表只留「未读 + 当前档位」的行', () => {
    expect(unreadRowsFor(rows(), 'urgent').map((r) => r.id)).toEqual([1])
    expect(unreadRowsFor(rows(), 'important').map((r) => r.id)).toEqual([2])
    expect(unreadRowsFor(rows(), 'general').map((r) => r.id)).toEqual([4])
  })
})

describe('挂载：真取数 + 真动作', () => {
  it('装载即发 `GET /api/business/notifications/`，未读表拿到「当前页签 + 未读」的行', async () => {
    const { root, unmount } = await mount()
    expect(calls[0]).toEqual({
      method: 'get',
      url: '/api/business/notifications/',
      params: { page: 1, page_size: 200 },
    })
    // 默认页签是「急需处理」（对标标记里 `layui-this` 就在 lay-id=20 上）⇒ 只有待审核那条未读。
    expect(lastUnreadRender()).toMatchObject({ elem: '#unReadTableList', toolbar: '#unReadtoolbarDemo' })
    expect((lastUnreadRender().data as MyMessageRow[]).map((r) => r.id)).toEqual([1])
    // 已读表恒空（对标那张表的 url 被注释掉，后端也没有 isRead 筛选）。
    expect(lastReadRender()).toMatchObject({ elem: '#readTableList' })
    expect(lastReadRender().data).toEqual([])
    // 徽标：三档各 1 条未读。
    expect(root.querySelector('.news20')!.textContent).toBe('1')
    expect(root.querySelector('.news0')!.textContent).toBe('1')
    unmount()
  })

  it('侧边页签切换 ⇒ 重取一次，并按新页签重画未读表', async () => {
    const { root, unmount } = await mount()
    const titles = root.querySelector('.sidetab > .layui-tab-title')!
    titles.querySelector('.layui-this')!.classList.remove('layui-this')
    titles.querySelector('[lay-id="0"]')!.classList.add('layui-this')
    titles.querySelector('[lay-id="0"]')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(h.renders.length).toBe(4))
    expect(calls).toHaveLength(2)
    expect((lastUnreadRender().data as MyMessageRow[]).map((r) => r.id)).toEqual([4])
    unmount()
  })

  it('侧边页签点击后**面板仍显示**（兜住 `initIslandElements` 的 `i === idx` 收口）', async () => {
    const { root, unmount } = await mount()
    const panel = root.querySelector('.sidetab > .layui-tab-content > .layui-tab-item')!
    // 复现共享渲染器的行为：它按 `i === idx` toggle 所有面板，点第 2 个会把唯一的面板关掉
    // （实测现象：整张表消失）。本页按对标的实测渲染把它显示回来。
    panel.classList.remove('layui-show')
    root
      .querySelector('.sidetab .layui-tab-title [lay-id="10"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(panel.classList.contains('layui-show')).toBe(true)
    unmount()
  })

  it('「标记为已读」对**勾选行**逐条发单条 read（URL 带各自 id）', async () => {
    const { unmount } = await mount()
    h.checked = [toRow(notification(4, '被指派'))]
    h.handlers.get('toolbar(unReadTableList)')!({ event: 'readBtn' })
    await vi.waitFor(() => expect(calls.some((c) => c.method === 'post')).toBe(true))
    await vi.waitFor(() => expect(h.renders.length).toBe(4))
    expect(calls.filter((c) => c.method === 'post')).toEqual([
      { method: 'post', url: '/api/business/notifications/4/read/' },
    ])
    expect(h.msgs).toEqual([]) // 对标成功后不弹提示，只重载
    unmount()
  })

  it('未勾选就点「标记为已读」⇒ 提示对标的「请勾选要操作的数据行」，**不发请求**', async () => {
    const { unmount } = await mount()
    h.checked = []
    h.handlers.get('toolbar(unReadTableList)')!({ event: 'readBtn' })
    await Promise.resolve()
    expect(h.msgs).toEqual(['请勾选要操作的数据行'])
    expect(calls.filter((c) => c.method === 'post')).toHaveLength(0)
    unmount()
  })

  it('「全部标记为已读」= 后端没有批量 ⇒ 对**每条未读**逐条 POST（已读的不发）', async () => {
    const { unmount } = await mount()
    h.handlers.get('toolbar(unReadTableList)')!({ event: 'readAll' })
    await vi.waitFor(() => expect(calls.filter((c) => c.method === 'post')).toHaveLength(3))
    expect(calls.filter((c) => c.method === 'post').map((c) => c.url).sort()).toEqual([
      '/api/business/notifications/1/read/',
      '/api/business/notifications/2/read/',
      '/api/business/notifications/4/read/',
    ])
    unmount()
  })

  it('点消息（`.viewMsg`）⇒ 开层（对标 layer 参数原值）+ 标记该条已读', async () => {
    const { root, unmount } = await mount()
    const link = document.createElement('a')
    link.className = 'viewMsg'
    link.dataset.id = '1'
    link.dataset.title = '待审核'
    link.dataset.content = '订单号：x'
    root.appendChild(link)
    link.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(h.opened[0]).toMatchObject({
      title: '待审核',
      type: 1,
      skin: 'layui-layer-lan',
      closeBtn: 1,
      anim: 2,
      shadeClose: true,
      area: ['80%', '400px'],
    })
    await vi.waitFor(() => expect(calls.filter((c) => c.method === 'post')).toHaveLength(1))
    expect(calls.find((c) => c.method === 'post')!.url).toBe('/api/business/notifications/1/read/')
    unmount()
  })

  it('「刷新」按钮重取一次；卸载后点击不再有反应', async () => {
    const { unmount } = await mount()
    h.handlers.get('toolbar(unReadTableList)')!({ event: 'refresh' })
    await vi.waitFor(() => expect(calls).toHaveLength(2))
    unmount()
    const link = document.createElement('a')
    link.className = 'viewMsg'
    link.dataset.id = '1'
    document.body.appendChild(link)
    link.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(h.opened).toHaveLength(0)
  })

  it('列表失败 ⇒ **如实**提示后端 msg，表格清空（不留假数据）', async () => {
    mockedRequest.mockReset()
    mockedRequest.mockImplementation((async () => {
      throw new BizError(1002, '无权访问该资源')
    }) as never)
    const { unmount } = await mount()
    expect(h.msgs).toEqual(['无权访问该资源'])
    expect(lastUnreadRender().data).toEqual([])
    expect(lastReadRender().data).toEqual([])
    unmount()
  })

  it('标记已读失败也如实提示，并**重取**（页面显示服务端真实状态）', async () => {
    const { unmount } = await mount()
    calls.length = 0
    mockedRequest.mockImplementation((async (config: Call) => {
      calls.push(config)
      if (config.method === 'post') throw new BizError(1002, '通知不存在')
      return { items: FIXTURES, total: FIXTURES.length, page: 1, page_size: 200 }
    }) as never)
    h.checked = [toRow(notification(4, '被指派'))]
    h.handlers.get('toolbar(unReadTableList)')!({ event: 'readBtn' })
    await vi.waitFor(() => expect(h.msgs).toEqual(['通知不存在']))
    // 失败后仍重取一次（1 次 POST + 1 次 GET），表格按服务端状态重画。
    await vi.waitFor(() => expect(calls.filter((c) => c.method === 'get')).toHaveLength(1))
    unmount()
  })

  it('「消息设置」里的单选与一键重置没有后端 ⇒ 提示「尚未开放」，**不发请求**', async () => {
    const { root, unmount } = await mount()
    calls.length = 0
    const radio = document.createElement('div')
    radio.className = 'layui-form-radio'
    const item = document.createElement('div')
    item.className = 'layui-form-item'
    item.innerHTML = '<label class="layui-form-label">系统新消息提醒</label>'
    item.appendChild(radio)
    root.appendChild(item)
    radio.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(h.msgs).toEqual(['「系统新消息提醒」尚未开放'])
    expect(calls).toHaveLength(0)
    unmount()
  })

  it('表格配置取自 `TABLE_SPECS`（没有另抄一份 limit/limits 常量）', async () => {
    const { unmount } = await mount()
    for (const spec of TABLE_SPECS) {
      const rendered = h.renders.find((r) => r.elem === spec.elem)!
      expect(rendered.toolbar).toBe(spec.toolbar)
      expect(rendered.defaultToolbar).toEqual(spec.defaultToolbar)
      expect(rendered.limit).toBeUndefined() // 分页配置走 `page` 对象
      expect(rendered.page).toEqual({ limit: spec.limit!, limits: spec.pageLimits! })
    }
    unmount()
  })
})
