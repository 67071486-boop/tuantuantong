import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import {
  AUDIT_ACTORS_URL,
  AUDIT_EVENTS_URL,
  AUDIT_LOGS_URL,
  fetchAuditActors,
  fetchAuditLogs,
  formatCreatedAt,
  LOG_TYPE_LABELS,
  ORDER_OPEN_EDIT,
  reportOrderOpenEdit,
  toAuditLogParams,
} from '../auditLogs'

const mockedRequest = vi.mocked(request)

beforeEach(() => {
  mockedRequest.mockReset()
  mockedRequest.mockResolvedValue({} as never)
})

describe('三个端点的路径是字面值（打错一个字就红）', () => {
  it('导出常量与后端 urls.py 注册的路径逐字一致（尾斜杠见 spec R18）', () => {
    expect(AUDIT_LOGS_URL).toBe('/api/audit/logs/')
    expect(AUDIT_ACTORS_URL).toBe('/api/audit/actors/')
    expect(AUDIT_EVENTS_URL).toBe('/api/audit/events/')
  })
})

describe('toAuditLogParams：六个筛选 + 分页的线上形状', () => {
  it('日期起止按 yyyy-MM-dd HH:mm:ss 原样透传，page/page_size 用项目分页形状', () => {
    expect(
      toAuditLogParams({
        stime: '2026-09-20 00:00:00',
        etime: '2026-09-21 23:59:59',
        page: 2,
        page_size: 30,
      }),
    ).toEqual({
      stime: '2026-09-20 00:00:00',
      etime: '2026-09-21 23:59:59',
      page: 2,
      page_size: 30,
    })
  })

  it('log_type=0（下拉的「全部」）不上线；真分类照传', () => {
    expect(toAuditLogParams({ log_type: 0 })).toEqual({})
    expect(toAuditLogParams({ log_type: 20 })).toEqual({ log_type: 20 })
  })

  it('actor 多值编码成逗号分隔的 username 串（**后端 API 契约**；页面是单选，R29）', () => {
 // 页面（对标 #recUser 单选）只会传一个元素；多个元素这条是钉后端的多值容忍，
 // 防止有人把这里的列表→join 收窄成单值（spec §6.1 的 R29 更正）。
    expect(toAuditLogParams({ actor: ['客服甲', '客服乙'] })).toEqual({
      actor: '客服甲,客服乙',
    })
    expect(toAuditLogParams({ actor: ['客服甲'] })).toEqual({ actor: '客服甲' })
    expect(toAuditLogParams({ actor: [] })).toEqual({})
    expect(toAuditLogParams({ actor: [' 客服甲 ', ''] })).toEqual({ actor: '客服甲' })
  })

  it('空串按「没传」丢掉；字符串值两侧空白也去掉', () => {
    expect(
      toAuditLogParams({
        stime: '',
        etime: '   ',
        game_name: '',
        order_no: '',
        key: '',
      }),
    ).toEqual({})
    expect(toAuditLogParams({ game_name: ' 原神 ', key: ' 改价 ' })).toEqual({
      game_name: '原神',
      key: '改价',
    })
  })

  it('ordering 两向透传；不传（或空串）时线上形状里没有这个键', () => {
 // 后端白名单的两个值（`LOG_ORDERING_FIELDS`），逐字透传。
    expect(toAuditLogParams({ ordering: 'created_at' })).toEqual({ ordering: 'created_at' })
    expect(toAuditLogParams({ ordering: '-created_at' })).toEqual({ ordering: '-created_at' })
 // 没点排序：形状与加本参数之前**逐字节相同**（空串也走同一支）。
    expect(toAuditLogParams({})).toEqual({})
  })
})

describe('两个读调用 + 上报调用的方法与参数（断言打在真实出口 request 的入参）', () => {
  it('列表走 GET /api/audit/logs/（六筛选齐全时参数逐字对齐后端序列化器）', async () => {
    await fetchAuditLogs({
      stime: '2026-09-20 00:00:00',
      etime: '2026-09-21 23:59:59',
      log_type: 0,
      game_name: '原神',
      actor: ['客服甲', '客服乙'],
      order_no: '20260921000000000001',
      key: '改价',
      page: 1,
      page_size: 30,
    })
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(mockedRequest.mock.calls[0]![0]).toEqual({
      method: 'get',
      url: '/api/audit/logs/',
      params: {
        stime: '2026-09-20 00:00:00',
        etime: '2026-09-21 23:59:59',
        game_name: '原神',
        actor: '客服甲,客服乙',
        order_no: '20260921000000000001',
        key: '改价',
        page: 1,
        page_size: 30,
      },
    })
  })

  it('ordering 经 fetchAuditLogs 原样上线（服务端排序参数，不额外加工）', async () => {
    await fetchAuditLogs({ ordering: 'created_at', page: 1 })
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(mockedRequest.mock.calls[0]![0]).toEqual({
      method: 'get',
      url: '/api/audit/logs/',
      params: { ordering: 'created_at', page: 1 },
    })
  })

  it('人员枚举走 GET /api/audit/actors/，只有 key 一个参数', async () => {
    await fetchAuditActors('张')
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(mockedRequest.mock.calls[0]![0]).toEqual({
      method: 'get',
      url: '/api/audit/actors/',
      params: { key: '张' },
    })
  })

  it('请求完整删除原因时才发送扩展字段参数', async () => {
    await fetchAuditLogs({ include_delete_reason: true })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/audit/logs/',
      params: { include_delete_reason: 'true' },
    })
  })

  it('人员枚举不传 key 时不发空串参数（后端把空串当「没传」，两端一致）', async () => {
    await fetchAuditActors()
    expect(mockedRequest.mock.calls[0]![0]).toEqual({
      method: 'get',
      url: '/api/audit/actors/',
      params: {},
    })
  })

  it('进入订单编辑态走 POST /api/audit/events/，载荷只有 action + order_no', async () => {
    await reportOrderOpenEdit('20260921000000000001')
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(mockedRequest.mock.calls[0]![0]).toEqual({
      method: 'post',
      url: '/api/audit/events/',
      data: { action: 'order.open_edit', order_no: '20260921000000000001' },
    })
 // 动作 code 是后端白名单里的那一个（`FRONTEND_REPORTABLE_READ_ACTIONS`）。
    expect(ORDER_OPEN_EDIT).toBe('order.open_edit')
  })
})

describe('created_at 格式化（交接自 Task 5：后端回 ISO，对标列是 Y-m-d H:i:s）', () => {
  it('带 +08:00 的 ISO 字面值 → 字面的 Y-m-d H:i:s', () => {
    expect(formatCreatedAt('2026-09-21T10:00:00+08:00')).toBe('2026-09-21 10:00:00')
  })

  it('带微秒的 ISO 也取到秒（DRF 的 isoformat 会带上微秒）', () => {
    expect(formatCreatedAt('2026-09-21T10:00:09.123456+08:00')).toBe('2026-09-21 10:00:09')
  })

  it('空值 / 非字符串 / 认不出的形状一律给空串，绝不把原始 ISO 填进列里', () => {
    expect(formatCreatedAt('')).toBe('')
    expect(formatCreatedAt(undefined)).toBe('')
    expect(formatCreatedAt(20260921)).toBe('')
    expect(formatCreatedAt('not-a-date')).toBe('')
  })
})

describe('日志分类词表照抄对标（8 项，code 一字不改）', () => {
  it('code → 中文与对标 `systemlog.js` 的 switch 逐条一致', () => {
    expect(LOG_TYPE_LABELS).toEqual({
      5: '号库相关',
      10: '订单相关',
      20: '绩效相关',
      30: '用户相关',
      40: '系统相关',
      50: '会员相关',
      60: '排团相关',
      70: '其它',
    })
  })
})
