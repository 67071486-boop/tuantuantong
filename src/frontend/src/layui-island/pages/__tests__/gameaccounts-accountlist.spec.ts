import { describe, expect, it } from 'vitest'
import { COLS, PAGE_LIMIT, PAGE_LIMITS } from '../gameaccounts-accountlist'
import { TABLE_ELEM, TOOLBAR_ELEM } from '../gameaccounts-accountlist'

/** 列的标题（`ArtifactCol` 的 `title` 是可选的）。 */
function titles(): string[] {
  return COLS.filter((c) => typeof c.title === 'string').map((c) => c.title as string)
}

/** 被隐藏的列标题（对标 `done` 回调的 `display:none` 也折成一个 `hide`）。 */
function hiddenTitles(): string[] {
  return COLS.filter((c) => c.hide === true).map((c) => (c.title as string) ?? '')
}

describe('账号管理中心 layui 页的列声明', () => {
  it('勾选列带 type:checkbox——缺它这一列会静默变成普通列（表头勾选框消失、宽度被撑开）', () => {
    // 探针的表头比对只读「文案 + 宽度」，空文案的特殊列丢了它**不会报**，所以要在这里断言。
    expect(COLS[0]?.field).toBeUndefined()
    expect((COLS[0] as unknown as { type?: string }).type).toBe('checkbox')
    expect(COLS[0]?.width).toBe(30)
    expect(COLS[0]?.fixed).toBe('left')
  })

  it('可见列 20 列（含勾选列），文案序列与对标 accountlist.js 的 cols[0] 一致', () => {
    // 对标实测：1650 视口下表头 20 个 `th`（勾选 + 19 个可见数据列）。
    const visible = COLS.filter((c) => c.hide !== true)
    expect(visible.length).toBe(20)
    expect(titles()).toEqual([
      '编号',
      '卖出/出租单号',
      '角色名',
      '阵营',
      '区服信息',
      '当前信息1',
      '当前信息2',
      '当前信息3',
      '标签',
      '游戏账号',
      '成本价',
      '售价',
      '利润',
      '账号状态',
      '用途类型',
      '接手人',
      '入库时间',
      '解/卖/租时',
      '账号截图',
      '备注',
      '机位',
      // 「操作」列没有 field（对标是 `{ fixed:'right', title:'操作', width:92 }`）
      '操作',
    ])
  })

  it('隐藏列 = 「成本价」「利润」（对标 cols 的 hide:true）+「阵营」（对标 done 回调的 display:none）', () => {
    // ⚠️ 曾经误把「游戏账号」也写成 hide:true（草稿的错），探针立刻报「表头 20 vs 19、第 9 列起错位」。
    // 「阵营」的 hide 不是来自 cols，而是对标 `done` 回调对非「魔兽世界」把它 `display:none`（见文件头注释）。
    expect(hiddenTitles().sort()).toEqual(['利润', '成本价', '阵营'].sort())
    expect(COLS.some((c) => c.field === 'gameAcc' && c.hide === true)).toBe(false)
    // 对标 cols 里**只有两处** `hide: true`（L273 costPrice / L282 profit）——用来钉住「别多加」。
    expect(COLS.filter((c) => c.hide === true)).toHaveLength(3)
  })

  it('3 个 minwidth 列（小写 w，layui 不认）**不补** width/minWidth 之外的写法——只保留 minWidth', () => {
    // 对标 `gameTF`/`otherLevel`/`otherMark` 只有 `minWidth`（没有 `width`）→ layui 眼里是自动列。
    // 给它们补一个 `width` 会把自动列撑开、总宽超视口（impl-brief §「值纪律」与 replicaPage 的告警）。
    const autoCols = ['gameTF', 'otherLevel', 'otherMark'].map((field) =>
      COLS.find((c) => c.field === field),
    )
    // 三个都必须找得到（找不到就是列名抄错了）
    expect(autoCols.map((c) => c?.field)).toEqual(['gameTF', 'otherLevel', 'otherMark'])
    // 都没有 width（只有 minWidth）
    expect(autoCols.map((c) => c?.width)).toEqual([undefined, undefined, undefined])
    expect(autoCols.map((c) => typeof c?.minWidth)).toEqual(['number', 'number', 'number'])
    expect(autoCols.map((c) => c?.minWidth)).toEqual([115, 110, 110])
  })

  it('分页：6 档 limits（含 100），取自 accountlist.js；limit 20', () => {
    // 出处：`.tools/recon/suqing1/assets/JS2019/GameAccounts/accountlist.js` 的
    // `, page: true` / `, limit: 20` / `, limits: [10, 20, 30, 40, 50,100]`。
    expect(PAGE_LIMITS).toEqual([10, 20, 30, 40, 50, 100])
    expect(PAGE_LIMIT).toBe(20)
  })

  it('工具栏/表格选择器与对标同值', () => {
    // 本页因 `done` 回调（把统计文案写进 `#total`）而用本地 `table.render`，
    // 没有导出 `TABLE` 对象——选择器对照这两个常量。
    expect(TABLE_ELEM).toBe('#tableList')
    expect(TOOLBAR_ELEM).toBe('#toolbarDemo')
  })
})
