import { describe, expect, it } from 'vitest'
import {
  ORDER_STATUS_LABELS,
  ORDER_STATUS_TAG_TYPES,
  ORDER_STATUS_VALUES,
  orderStatusLabel,
  orderStatusTagType,
} from '../orderStatus'

describe('订单状态镜像', () => {
  it('8 个状态齐全，声明顺序与后端 OrderStatus 一致', () => {
    expect(ORDER_STATUS_VALUES).toEqual([
      'UNASSIGNED',
      'WAITING_GRAB',
      'IN_PROGRESS',
      'PENDING_REVIEW',
      'COMPLETED',
      'ISSUE',
      'PAUSED',
      'REFUNDED',
    ])
  })

  it('中文 label 与后端 models.py 的 OrderStatus 逐字一致', () => {
    expect(ORDER_STATUS_LABELS).toEqual({
      UNASSIGNED: '未分配',
      WAITING_GRAB: '待抢单',
      IN_PROGRESS: '代练中',
      PENDING_REVIEW: '待验收',
      COMPLETED: '已完成',
      ISSUE: '问题单',
      PAUSED: '已暂停',
      REFUNDED: '已退单',
    })
  })

  it('tag 类型只取 EP 的语义值，且 issue/refunded 是 danger', () => {
    expect(ORDER_STATUS_TAG_TYPES).toEqual({
      UNASSIGNED: 'info',
      WAITING_GRAB: 'warning',
      IN_PROGRESS: 'primary',
      PENDING_REVIEW: 'warning',
      COMPLETED: 'success',
      ISSUE: 'danger',
      PAUSED: 'info',
      REFUNDED: 'danger',
    })
  })

  // ⚠️ 期望值必须是**字面量**，不能写 ORDER_STATUS_LABELS[s]——那等于用实现验实现，
  // 会把「访问器写错/少一个分支」整个放过去（评审 Important 2 实测过这个洞）。
  it('每个状态都能取到非空 label（按具体值断言，不用真值判断）', () => {
    expect(orderStatusLabel('UNASSIGNED')).toBe('未分配')
    expect(orderStatusLabel('WAITING_GRAB')).toBe('待抢单')
    expect(orderStatusLabel('IN_PROGRESS')).toBe('代练中')
    expect(orderStatusLabel('PENDING_REVIEW')).toBe('待验收')
    expect(orderStatusLabel('COMPLETED')).toBe('已完成')
    expect(orderStatusLabel('ISSUE')).toBe('问题单')
    expect(orderStatusLabel('PAUSED')).toBe('已暂停')
    expect(orderStatusLabel('REFUNDED')).toBe('已退单')
  })

  // ⚠️ 补上**已知**状态的 tag 访问器：此前只有 'BOGUS_STATUS' 走过这个函数，
  // 于是 orderStatusTagType('ISSUE') 返回 'info' 也能全绿（评审 Important 2）。
  it('已知状态通过访问器取到对应的语义 tag 类型（字面量断言）', () => {
    expect(orderStatusTagType('IN_PROGRESS')).toBe('primary')
    expect(orderStatusTagType('COMPLETED')).toBe('success')
    expect(orderStatusTagType('ISSUE')).toBe('danger')
    expect(orderStatusTagType('REFUNDED')).toBe('danger')
  })

  it('后端加了前端还没同步的状态时原样显示，不崩也不显示 undefined', () => {
    expect(orderStatusLabel('BOGUS_STATUS')).toBe('BOGUS_STATUS')
    expect(orderStatusTagType('BOGUS_STATUS')).toBe('info')
  })
})
