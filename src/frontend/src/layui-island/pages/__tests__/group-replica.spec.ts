// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { COLS_FOR_TEST as MYGROUP_COLS } from '../group-mygroup'
import { COLS_FOR_TEST as ADDEDIT_COLS } from '../group-addeditgroup'
import { COLS_FOR_TEST as GROUPLIST_COLS } from '../group-grouplist'
import { COLS_FOR_TEST as GROUPLIST2025_COLS } from '../group-grouplist2025'
import {
  COLS_FOR_TEST as ORDER_COLS,
  buildPublicCols,
  buildTragCols,
} from '../group-grouplist2020'

/**
 * 6 个「游戏排团」复刻页的**表头防漂移测试**。
 *
 * 断言对象是**对标产物**（不是我们的实现），逐页指到源文件行号：
 *  - `.tools/recon/suqing1/assets/JS2019/Group/mygroup.js` L33-106
 *  - 同目录 `addeditgroup.js` L170-212、`grouplist.js` L119-215、`grouplist2025.js` L119-194、
 *    `grouplist2020.js` L692-715 / L1418-1537 / L1948-1968
 *
 * 为什么值得写（三处探针量不到的死角）：
 *  1. `group-addeditgroup` 的 `#tableList` 父容器 `.jobDiv` 在对标里是**隐藏**的
 *     （`addeditgroup.js` L30-32，`defGame` 不含「魔兽」）→ 探针 th 全为空，列宽/列序抄错不会报错；
 *  2. `group-grouplist2020` 的 `#tableList_public` / `#moveOrderTable` 初始态**根本不 render**
 *     （见 `pages/group-grouplist2020.ts` 顶部三表对照表）→ 探针完全看不到它们；
 *  3. `#moveOrderTable` 的 `cols` 是**运行期变量** `tragTableCols`（L1948），
 *     草稿由脚本静态解析——**本测试就是那次「人工核对」的固化**（结论：静态解析与页面 JS 一致）。
 */
describe('复刻页表头（逐页对齐对标 cols）', () => {
  const titles = (cols: Record<string, unknown>[]): unknown[] => cols.map((c) => c.title)

  /**
   * `cols[].type` 的**逐页核对**（主会话 2026-09-20 要求）。
   * 判据：`grep -n "type: *'" <页面JS>` 在 `cols` 里出现的才是特殊列。实测结果：
   *   mygroup L34 / grouplist L120 / grouplist2025 L120 / grouplist2020 L1420 —— 各 1 个 `checkbox`；
   *   addeditgroup、`#tableList_public`、`#moveOrderTable`、jdppublictask 的 per-card 表 —— **0 个**。
   * 漏写会让表头勾选框**静默消失**（parity 只比文案+宽度，空文案列丢了不报），所以钉死。
   */
  it('特殊列 `type` 逐页与对标一致（4 个 checkbox，其余 0）', () => {
    expect(MYGROUP_COLS.filter((c) => c.type !== undefined).map((c) => c.type)).toEqual(['checkbox'])
    expect(GROUPLIST_COLS.filter((c) => c.type !== undefined).map((c) => c.type)).toEqual(['checkbox'])
    expect(GROUPLIST2025_COLS.filter((c) => c.type !== undefined).map((c) => c.type)).toEqual(['checkbox'])
    expect(ORDER_COLS.filter((c) => c.type !== undefined).map((c) => c.type)).toEqual(['checkbox'])
    expect(ADDEDIT_COLS.some((c) => c.type !== undefined)).toBe(false)
    expect(buildPublicCols().some((c) => c.type !== undefined)).toBe(false)
    expect(buildTragCols().some((c) => c.type !== undefined)).toBe(false)
  })

  it('group-mygroup：11 列（checkbox + 10），顺序与文案对齐 mygroup.js L34-105', () => {
    expect(titles(MYGROUP_COLS)).toEqual([
      undefined,
      '排团编号',
      '排团名称',
      '游戏区服/阵营',
      '团队指挥',
      '开始时间',
      '完成时间',
      '进度',
      '排团标识',
      '配号数',
      '备注',
    ])
    expect(MYGROUP_COLS[0]).toMatchObject({ type: 'checkbox', fixed: 'left', width: 30 })
    // 对标 L36/L48/L104 的 `unresize:true` 只有这 3 列。
    expect(MYGROUP_COLS.filter((c) => c.unresize === true).length).toBe(3)
  })

  it('group-addeditgroup：8 列；`在小程序上卖价` 按 wechatApp=\'N\' 取 hide:true（人工定值）', () => {
    expect(titles(ADDEDIT_COLS)).toEqual([
      '序号',
      '坑位职业',
      '最低装等要求数',
      '天赋/盟约',
      '在小程序上卖价',
      '职业备注',
      '坑位打手',
      '操作',
    ])
    const price = ADDEDIT_COLS.find((c) => c.field === 'postionPrice')
    expect(price?.hide).toBe(true)
    // 对标 L194 是 `hide: wechatApp=='Y'?false:true`；HTML L277 的隐藏域是 value="N"。
    expect(price?.width).toBe(100)
  })

  it('group-grouplist：15 列（checkbox + 14），`taskId` 左固定、末列挂 #baroperating', () => {
    expect(titles(GROUPLIST_COLS)).toEqual([
      undefined,
      '排团编号',
      '排团名称',
      '项目/副本名称',
      '游戏区服/阵营',
      '团队指挥',
      '开团时间',
      '完成时间',
      '进度',
      '排团标识',
      '配号数',
      '每坑价格',
      '排序(越大越前)',
      '备注',
      '操作',
    ])
    expect(GROUPLIST_COLS[1]).toMatchObject({ field: 'taskId', width: 70, fixed: 'left' })
    // 对标 L214 的 `fixed: ''` = 不固定 → 实现里**不应**出现 fixed。
    expect(GROUPLIST_COLS[14]?.fixed).toBeUndefined()
    expect(GROUPLIST_COLS[14]?.toolbar).toBe('#baroperating')
  })

  it('group-grouplist2025：13 列；`缺口` 用**有效**的 minWidth（大写 W，L182）', () => {
    expect(titles(GROUPLIST2025_COLS)).toEqual([
      undefined,
      '排团编号',
      '排团名称',
      '游戏',
      '开团时间',
      '完成时间',
      '进度',
      '坑位数',
      '已配号数',
      '缺口',
      '排序',
      '备注',
      '操作',
    ])
    const lack = GROUPLIST2025_COLS.find((c) => c.field === 'lackJobName')
    expect(lack?.minWidth).toBe(80)
    expect(lack?.width).toBeUndefined()
  })

  it('group-grouplist2020 #orderList：12 列；`nresize`（拼写错）不得被写进实现', () => {
    expect(titles(ORDER_COLS)).toEqual([
      undefined,
      '操作',
      '角色名',
      '阵营',
      '代练项目',
      '角色信息',
      '订单状态',
      '订单来源',
      '接单价',
      '打手',
      '重要备注',
      '录单时间',
    ])
    expect(ORDER_COLS[0]).toMatchObject({ type: 'checkbox', fixed: 'left', width: 30, hide: true })
    expect(ORDER_COLS.some((c) => 'nresize' in c)).toBe(false)
    expect(ORDER_COLS.some((c) => c.unresize === true)).toBe(false)
  })

  it('group-grouplist2020 #tableList_public：6 列（只在点「公开坑位」时才 render）', () => {
    expect(titles(buildPublicCols())).toEqual(['职业', '装等要求', '天赋/盟约', '坑位备注', '是否已公开', '操作'])
  })

  it('group-grouplist2020 #moveOrderTable：运行期 `tragTableCols`（L1948-1968）静态解析 = 6 列、全部无宽', () => {
    const cols = buildTragCols()
    expect(cols.map((c) => c.field)).toEqual([
      'recUser',
      'gameRoleName',
      'gameJob',
      'gameGroup',
      'itemRemark',
      'dlProject',
    ])
    expect(titles(cols)).toEqual(['打手', '角色名', '职业', '阵营', '装等', '需求'])
    // 对标这 6 列**都没有 width** → layui 自动列。补 width 会把总宽撑破。
    expect(cols.every((c) => c.width === undefined)).toBe(true)
  })
})
