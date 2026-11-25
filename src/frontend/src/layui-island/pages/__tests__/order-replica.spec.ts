// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import type { ArtifactCol, ArtifactTable } from '@/layui/artifactPage'
import { COLS as MYORDER_COLS, PAGE_LIMITS as MYORDER_LIMITS, TABLE as MYORDER_TABLE } from '../order-myorderlist'
import {
  COLS as INSIDE_COLS,
  PAGE_LIMITS as INSIDE_LIMITS,
  TABLE as INSIDE_TABLE,
} from '../order-insiderecorderlist'

/**
 * Order 批 3 页的**表头/表格配置防漂移测试**。
 *
 * 断言对象是**对标产物**（不是我们的实现）：期望值逐条手抄自
 * `.tools/recon/suqing1/assets/JS2019/Order/{myorderlist,insiderecorderlist}.js`
 * 的 `cols`，并在写页面前后逐列核对过。
 *
 * 2026-09-21 业务裁剪（D-0036）删掉「交易平台订单处理」（`order-coreordercenter`）与
 * 「客户填单生成」（`order-clientordergenerate`）两页及其两个 describe，
 * 本文件从 5 页收窄到 3 页。
 * ⭐ 2026-10-30（菜单两改线，D-0339）：「异常消失订单」（`order-exceptionorderlog`）整页删除
 * （业务方裁定：菜单和功能整个去掉；island 页与证据件同批删除、对标 key 登记 `REPLICA_SKIP` 第 9 类）
 * ⇒ 其 describe 与 imports 同一次删除，本文件从 3 页收窄到 2 页。
 *
 * 为什么值得写（本批最贵的三个静默陷阱）：
 *  1. 特殊列 `type:'checkbox'`（`myorderlist` L736）：漏了表头勾选框会**静默消失**，
 *     `parity` 的表头比对只读文案 + 宽度，看不见勾选框，也不看空文案列。
 *  2. 列级 `toolbar`（`myorderlist` L926）：漏了操作列变空列，同样不报。
 *  3. `hide` 的运行期表达式（`myorderlist` L776/L784/L823）：改错任一方向都会
 *     多一列/少一列可见列。运行期定值的依据（隐藏域 `value="N"`）写在页面模块注释里。
 *
 * `title` 为 `''` 的两列是**对标字典里真的没有那个 key**（`i18nDataList.property` /
 * `actionorderHoursDescStrcol`），不是漏搬 —— 见 `order-myorderlist.ts` /
 * `order-insiderecorderlist.ts` 的文件头注释。
 */
const titles = (cols: ArtifactCol[]): (string | undefined)[] => cols.map((c) => c.title)

/** 列声明里我们关心的全部字段（`undefined` 也照实断言，避免「以为没写其实写了」）。 */
const shape = (cols: ArtifactCol[]): unknown[] =>
  cols.map((c) => ({
    t: c.type,
    f: c.field,
    w: c.width,
    mw: c.minWidth,
    h: c.hide,
    fx: c.fixed,
    tb: c.toolbar,
  }))

const pageOf = (t: ArtifactTable): unknown => t.page

describe('order-myorderlist（对标 myorderlist.js L735-930）', () => {
  it('20 列，文案序列与对标一致（含 1 列空文案：i18nDataList.property 不存在）', () => {
    expect(MYORDER_COLS.length).toBe(20)
    expect(titles(MYORDER_COLS)).toEqual([
      undefined,
      '订单号',
      '系统内置单号',
      '角色名',
      '重要备注',
      '阵营/平台',
      '游戏名称',
      '代练/陪玩内容',
      '角色信息',
      '订单状态',
      '时间',
      '旺旺/来源',
      '接手人',
      '接手价',
      '订单截图',
      '接手时间',
      '机位',
      '',
      '日志',
      '操作',
    ])
  })

  it('勾选列 / 3 个 hide 列 / 操作列 toolbar 逐条对齐', () => {
    expect(shape(MYORDER_COLS)).toEqual([
      { t: 'checkbox', f: undefined, w: 30, mw: undefined, h: undefined, fx: 'left', tb: undefined },
      { t: undefined, f: 'orderIdView', w: 180, mw: undefined, h: undefined, fx: undefined, tb: undefined },
      { t: undefined, f: 'orderId', w: 150, mw: undefined, h: true, fx: undefined, tb: undefined },
      { t: undefined, f: 'roleName', w: 120, mw: undefined, h: undefined, fx: undefined, tb: undefined },
      { t: undefined, f: 'orderRemark', w: undefined, mw: undefined, h: true, fx: undefined, tb: undefined },
      { t: undefined, f: 'gameGroup', w: 55, mw: undefined, h: undefined, fx: undefined, tb: undefined },
      { t: undefined, f: 'gameName', w: 75, mw: undefined, h: undefined, fx: undefined, tb: undefined },
      { t: undefined, f: 'project', w: undefined, mw: 260, h: undefined, fx: undefined, tb: undefined },
      { t: undefined, f: 'gameInfo', w: undefined, mw: undefined, h: undefined, fx: undefined, tb: undefined },
      { t: undefined, f: 'orderSta', w: 65, mw: undefined, h: undefined, fx: undefined, tb: undefined },
      { t: undefined, f: 'orderHoursDescStr', w: 80, mw: undefined, h: undefined, fx: undefined, tb: undefined },
      { t: undefined, f: 'orderSoure', w: 90, mw: undefined, h: true, fx: undefined, tb: undefined },
      { t: undefined, f: 'recMan', w: 70, mw: undefined, h: undefined, fx: undefined, tb: undefined },
      { t: undefined, f: 'recFee', w: 70, mw: undefined, h: undefined, fx: undefined, tb: undefined },
      { t: undefined, f: '', w: 70, mw: undefined, h: undefined, fx: undefined, tb: undefined },
      { t: undefined, f: 'recOrderTime', w: 80, mw: undefined, h: undefined, fx: undefined, tb: undefined },
      { t: undefined, f: 'jiwei', w: 80, mw: undefined, h: undefined, fx: undefined, tb: undefined },
      // L845：`title: i18nDataList.property`（key 不存在 → 空文案），L846：`hide: remarkLocation=='Y'?true:false` + `#remarkLocation="N"` → 可见。
      { t: undefined, f: 'orderRemark', w: 80, mw: undefined, h: undefined, fx: undefined, tb: undefined },
      { t: undefined, f: 'dynamicRemark', w: 70, mw: undefined, h: undefined, fx: undefined, tb: undefined },
      // L926：`fixed:''` 是 falsy → 不写；`toolbar:'#baroperating'` 必须留。
      { t: undefined, f: '', w: 80, mw: undefined, h: undefined, fx: undefined, tb: '#baroperating' },
    ])
  })

  it('分页：page:true + limit 20 + limits [10,20,30,40,50]（L928-930）', () => {
    expect(MYORDER_LIMITS).toEqual([10, 20, 30, 40, 50])
    expect(pageOf(MYORDER_TABLE)).toEqual({ limit: 20, limits: MYORDER_LIMITS })
    expect(MYORDER_TABLE.toolbar).toBe('#toolbarDemo')
    expect(MYORDER_TABLE.defaultToolbar).toEqual(['filter'])
  })
})

describe('order-insiderecorderlist（对标 insiderecorderlist.js L551-669）', () => {
  it('15 列，文案序列与对标一致（含 1 列空文案：actionorderHoursDescStrcol 不存在）', () => {
    expect(INSIDE_COLS.length).toBe(15)
    expect(titles(INSIDE_COLS)).toEqual([
      '操作',
      '订单号',
      '类型',
      '角色名',
      '阵营/平台',
      '代练/陪玩项目',
      '角色信息',
      '订单状态',
      '',
      '接手价',
      '保证金要求',
      '参考图',
      '备注要求',
      '发布时间',
      '操作',
    ])
  })

  it('无勾选列；两张自动列是 minWidth（290 / 180），不是 width', () => {
    expect(INSIDE_COLS.some((c) => c.type === 'checkbox')).toBe(false)
    expect(INSIDE_COLS[5]?.field).toBe('project')
    expect(INSIDE_COLS[5]?.minWidth).toBe(290)
    expect(INSIDE_COLS[5]?.width).toBeUndefined()
    expect(INSIDE_COLS[6]?.field).toBe('gameInfo')
    expect(INSIDE_COLS[6]?.minWidth).toBe(180)
    expect(INSIDE_COLS[6]?.width).toBeUndefined()
  })

  it('分页：page:true + limit 20 + limits [10,20]（L667-669，只有两档）', () => {
    expect(INSIDE_LIMITS).toEqual([10, 20])
    expect(pageOf(INSIDE_TABLE)).toEqual({ limit: 20, limits: INSIDE_LIMITS })
    // L549 是对标自己的悬空引用（HTML 里没有 #toolbarDemo 模板）——照抄，补模板反而多出按钮。
    expect(INSIDE_TABLE.toolbar).toBe('#toolbarDemo')
    expect(INSIDE_TABLE.defaultToolbar).toEqual(['filter'])
  })
})
