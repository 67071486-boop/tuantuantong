// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import type { AuditLogRow } from '@/api/auditLogs'
import {
  buildLogCols,
  defaultLogDateRange,
  logTypeLabel,
  SORT_FIELD,
  toLogOrdering,
  toLogRow,
  toLogSearchParams,
} from '../system-systemlog'

type Col = Record<string, unknown>
type Templet = (row: Record<string, unknown>) => string

function byTitle(cols: Col[]): Map<string, Col> {
  return new Map(cols.filter((c) => typeof c.title === 'string').map((c) => [c.title as string, c]))
}

/** 一行接口原样数据（字段名逐条来自 `OperationLogRowSerializer`）。 */
const API_ROW: AuditLogRow = {
  target_id: '20260921000000000001',
  log_type: 10,
  source_order_no: 'TB-8899',
  order_no: '20260921000000000001',
  game_role_name: '旅行者',
  game_name: '原神',
  summary: '录入了订单：20260921000000000001，来源单号：TB-8899，发单价：95.00',
  actor: '客服甲',
  created_at: '2026-09-21T10:00:00+08:00',
  action: 'order.create',
  actor_display: '测试昵称丙',
}

describe('「来源订单号」列 = 来源号优先、内置单号兜底（D-0341，业务方 2026-10-30）', () => {
  const soureCell = (row: AuditLogRow): string => {
    const col = byTitle(buildLogCols()).get('来源订单号')!
    return (col.templet as Templet)(toLogRow(row))
  }

  it('来源号非空 ⇒ 显示来源号（不显示内置号）', () => {
    const html = soureCell(API_ROW)
    expect(html).toContain('TB-8899')
    expect(html).not.toContain('20260921000000000001')
  })

  it('来源号为空 ⇒ 兜底显示内置单号 `order_no`（不再是空格）', () => {
    const html = soureCell({ ...API_ROW, source_order_no: '' })
    expect(html).toContain('20260921000000000001')
  })

  it('兜底值同样过转义（单号是请求来源的文本，templet 返回值被当 HTML 插入）', () => {
    const html = soureCell({ ...API_ROW, source_order_no: '', order_no: '<b>x</b>' })
    expect(html).not.toContain('<b>')
    expect(html).toContain('&lt;b&gt;')
  })
})

describe('「操作用户」列显示昵称（spec §11.2）', () => {
  it('优先取 actor_display；缺这个键时回退 actor（老接口/老桩数据不炸）', () => {
    const col = byTitle(buildLogCols()).get('操作用户')!
    expect(
      (col.templet as Templet)(toLogRow({ ...API_ROW, actor_display: '测试昵称丙' })),
    ).toContain('测试昵称丙')
    // 老接口/老桩数据没有这个键：回退到行里存的账号名，**不是**空白。
    const legacy = { ...API_ROW } as Record<string, unknown>
    delete legacy.actor_display
    expect((col.templet as Templet)(toLogRow(legacy as AuditLogRow))).toContain('客服甲')
  })
})

describe('系统跟踪日志页的列定义（宽度/顺序是复刻值，不改）', () => {
  it('8 列，标题与宽度逐条等于对标 `systemlog.js` 的 cols', () => {
    const cols = buildLogCols()
    expect(cols.map((c) => c.title)).toEqual([
      '相关ID',
      '日志分类',
      '来源订单号',
      '订单角色名',
      '所属游戏',
      '操作说明',
      '操作用户',
      '操作时间',
    ])
    expect(cols.map((c) => c.width)).toEqual([150, 70, 200, 150, 100, undefined, 120, 130])
    // 「操作说明」是对标里唯一没有 width 的自动列；「操作时间」是唯一 sort:true 的列。
    expect(byTitle(cols).get('操作说明')!.width).toBeUndefined()
    expect(byTitle(cols).get('操作时间')!.sort).toBe(true)
    expect(byTitle(cols).get('操作时间')!.unresize).toBe(true)
  })
})

describe('「相关ID」列：渲染 target_id，纯文本不跳转（对标的 javascript:void(0) 锚）', () => {
  it('值出现在单元格里，href 只有 javascript:void(0)（没有可导航的目标）', () => {
    const col = byTitle(buildLogCols()).get('相关ID')!
    const html = (col.templet as Templet)(toLogRow(API_ROW))
    expect(html).toContain('20260921000000000001')
    expect(html).toContain('href="javascript:void(0);"')
    expect(html).not.toMatch(/href="\/|href="https?:|data-order=|data-id=/)
  })

  it('值里的 HTML 被转义（summary/actor/target_id 都是请求来源的文本）', () => {
    const col = byTitle(buildLogCols()).get('相关ID')!
    const html = (col.templet as Templet)(
      toLogRow({ ...API_ROW, target_id: '<img src=x onerror=alert(1)>' }),
    )
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img')
  })
})

describe('「操作说明」列渲染 summary；未知动作不炸（页面不用 action 渲染）', () => {
  it('summary 原样出现在单元格里', () => {
    const col = byTitle(buildLogCols()).get('操作说明')!
    const html = (col.templet as Templet)(toLogRow(API_ROW))
    expect(html).toContain(API_ROW.summary)
  })

  it('action 是不认识的新取值时整行照常渲染', () => {
    const row = toLogRow({ ...API_ROW, action: 'something.brand_new' }) as unknown as Record<
      string,
      unknown
    >
    const cols = buildLogCols()
    const rendered = cols.map((c) =>
      typeof c.templet === 'function'
        ? (c.templet as Templet)(row)
        : String(row[c.field as string] ?? ''),
    )
    expect(rendered).toHaveLength(8)
    expect(rendered.join('')).toContain(API_ROW.summary)
    expect(rendered[1]).toBe('订单相关')
  })
})

describe('「日志分类」列的数值→中文（照抄对标的 switch，默认「全部」）', () => {
  it('8 个 code 各自的中文', () => {
    expect(logTypeLabel(5)).toBe('号库相关')
    expect(logTypeLabel(10)).toBe('订单相关')
    expect(logTypeLabel(20)).toBe('绩效相关')
    expect(logTypeLabel(30)).toBe('用户相关')
    expect(logTypeLabel(40)).toBe('系统相关')
    expect(logTypeLabel(50)).toBe('会员相关')
    expect(logTypeLabel(60)).toBe('排团相关')
    expect(logTypeLabel(70)).toBe('其它')
  })

  it('认不出的 code 回退成对标的默认值「全部」，不空白也不抛', () => {
    expect(logTypeLabel(999)).toBe('全部')
    expect(logTypeLabel(undefined)).toBe('全部')
  })
})

describe('「操作时间」列：ISO 进去，Y-m-d H:i:s 出来', () => {
  it('toLogRow 就把 created_at 格式化好（列 templet 不再直接吃 ISO）', () => {
    expect(toLogRow(API_ROW).createTime).toBe('2026-09-21 10:00:00')
  })

  it('单元格渲染的是格式化后的值，不是原始 ISO 串', () => {
    const col = byTitle(buildLogCols()).get('操作时间')!
    const html = (col.templet as Templet)(toLogRow(API_ROW))
    expect(html).toContain('2026-09-21 10:00:00')
    expect(html).not.toContain('2026-09-21T10:00:00')
  })
})

describe('筛选栏 DOM 名 → 接口参数名（六个筛选）', () => {
  it('记录日期起止 / 日志分类 / 游戏 / 相关人员 / 系统订单号 / 搜索关键词', () => {
    expect(
      toLogSearchParams({
        sDate: '2026-09-20 00:00:00',
        eDate: '2026-09-21 23:59:59',
        logType: '20',
        gameName: '原神',
        recUser: '客服甲',
        key1: '改价',
        key2: '20260921000000000001',
      }),
    ).toEqual({
      stime: '2026-09-20 00:00:00',
      etime: '2026-09-21 23:59:59',
      log_type: 20,
      game_name: '原神',
      // 对标 #recUser 是单选（R29）⇒ 页面只给一个 username；列表形状是给后端多值容忍留的接口。
      actor: ['客服甲'],
      order_no: '20260921000000000001',
      key: '改价',
    })
  })

  it('相关人员不拆逗号：值里有逗号也原样当一个 username（页面是单选，R29）', () => {
    expect(toLogSearchParams({ recUser: '甲,乙' }).actor).toEqual(['甲,乙'])
  })

  it('排序状态经第二个入口参数汇进参数对象；没传时是 undefined（不发参数）', () => {
    expect(toLogSearchParams({}, 'created_at').ordering).toBe('created_at')
    expect(toLogSearchParams({}, '-created_at').ordering).toBe('-created_at')
    // `undefined` 而不是空串：`toAuditLogParams` 对两者都丢，但形状上一个是「没传」。
    expect(toLogSearchParams({}).ordering).toBeUndefined()
  })

  it('日志分类选 0（全部）时把 0 交给参数组装层去丢；空白的筛选留空串（由组装层丢）', () => {
    expect(toLogSearchParams({ logType: '0' }).log_type).toBe(0)
    expect(
      toLogSearchParams({
        sDate: '',
        eDate: '',
        gameName: '',
        recUser: '',
        key1: '',
        key2: '',
      }),
    ).toEqual({
      stime: '',
      etime: '',
      log_type: undefined,
      game_name: '',
      actor: [],
      order_no: '',
      key: '',
    })
  })
})

describe('「记录日期」的默认区间：对标服务端渲染值的重建（快照日 2026-09-20）', () => {
  it('昨天 00:00:00 ~ 今天 23:59:59（不是照抄会过期的固定日期）', () => {
    expect(defaultLogDateRange(new Date(2026, 8, 20, 12, 34, 56))).toEqual({
      sDate: '2026-09-19 00:00:00',
      eDate: '2026-09-20 23:59:59',
    })
  })

  it('跨月也按自然日回退（不出现 9 月 0 日这种串）', () => {
    expect(defaultLogDateRange(new Date(2026, 9, 1, 0, 0, 0))).toEqual({
      sDate: '2026-09-30 00:00:00',
      eDate: '2026-10-01 23:59:59',
    })
  })
})

describe('layui 排序事件 → 后端 ordering 词表（两向显式映射，认不出就不发）', () => {
  it('认的列就是「操作时间」列（`sort: true` 的那列）的 field', () => {
    const sortable = buildLogCols().filter((c) => c.sort === true)
    expect(sortable.map((c) => c.field)).toEqual([SORT_FIELD])
    expect(SORT_FIELD).toBe('createTime')
  })

  it('升序 → created_at；降序 → -created_at', () => {
    expect(toLogOrdering('createTime', 'asc')).toBe('created_at')
    expect(toLogOrdering('createTime', 'desc')).toBe('-created_at')
  })

  it('别的列 / 第三次点击的取消排序（type 为 null）/ 认不出的 type → undefined（不发参数）', () => {
    expect(toLogOrdering('userName', 'asc')).toBeUndefined()
    expect(toLogOrdering('createTime', null)).toBeUndefined()
    expect(toLogOrdering('createTime', undefined)).toBeUndefined()
    expect(toLogOrdering(undefined, 'desc')).toBeUndefined()
  })
})
