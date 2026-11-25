import { describe, expect, it } from 'vitest'

import {
  claimTargetOf,
  hallOrderNoDisplay,
  hallRowKey,
  hallServerDisplay,
  isClaimableMirrorPk,
} from '../hallDisplay'

/**
 * 大厅卡片纯函数的单测：区服**两源合并**（原生映射 / upstream 直用 / 映射不到 → 「—」）；抢单**分流**（先分 `origin` 再判 pk，对齐桌面 `claimTargetOf`）：`mirror_pk=5` → upstream、`mirror_pk=null` → 原生、`mirror_pk='x'`（原生行）→ 原生，upstream 行 pk 不可用 → null（fail-closed 数据门，哪怕单号非空）。夹具一律合成值（E-0077）。
 */

const resolve = (id: number | null): string => (id === 501 ? '测试区服一' : '')

describe('hallServerDisplay（区服两源合并）', () => {
  it('原生行：server 主键经字典映射', () => {
    expect(
      hallServerDisplay(
        { origin: 'native', server: 501, remote_game_server: '' },
        resolve,
      ),
    ).toBe('测试区服一')
  })

  it('原生行：映射不到（null / 字典缺）→ 「—」', () => {
    expect(hallServerDisplay({ origin: 'native', server: null, remote_game_server: '' }, resolve)).toBe('—')
    expect(hallServerDisplay({ origin: 'native', server: 999, remote_game_server: '' }, resolve)).toBe('—')
  })

  it('upstream 行：直用行内 remote_game_server（⛔ 不查我方字典）', () => {
    expect(
      hallServerDisplay(
        { origin: 'upstream', server: null, remote_game_server: '测试远端区服甲' },
        resolve,
      ),
    ).toBe('测试远端区服甲')
  })

  it('upstream 行：远端文本为空 → 「—」（不回落查字典）', () => {
    expect(hallServerDisplay({ origin: 'upstream', server: null, remote_game_server: '' }, resolve)).toBe('—')
    expect(hallServerDisplay({ origin: 'upstream', server: null, remote_game_server: '  ' }, resolve)).toBe('—')
  })

  it('D-0303：peer 行与 upstream 同口径——直用行内 remote_game_server（⛔ 不查我方字典）', () => {
    expect(
      hallServerDisplay(
        { origin: 'peer', server: null, remote_game_server: '测试团团通区服甲' },
        resolve,
      ),
    ).toBe('测试团团通区服甲')
    // 团团通载荷里**没有**区服文本（`peer_hall.serialize_peer_row` 恒填空串）⇒ 如实「—」。
    expect(hallServerDisplay({ origin: 'peer', server: null, remote_game_server: '' }, resolve)).toBe('—')
  })
})

describe('isClaimableMirrorPk（正整数收窄）', () => {
  it('正整数通过；null / 0 / 负数 / 小数 / 串 / NaN 一律拒绝', () => {
    expect(isClaimableMirrorPk(5)).toBe(true)
    expect(isClaimableMirrorPk(1)).toBe(true)
    expect(isClaimableMirrorPk(null)).toBe(false)
    expect(isClaimableMirrorPk(undefined)).toBe(false)
    expect(isClaimableMirrorPk(0)).toBe(false)
    expect(isClaimableMirrorPk(-3)).toBe(false)
    expect(isClaimableMirrorPk(2.5)).toBe(false)
    expect(isClaimableMirrorPk('5')).toBe(false)
    expect(isClaimableMirrorPk(Number.NaN)).toBe(false)
  })
})

describe('claimTargetOf（抢单分流，先分 origin 再判 pk——对齐桌面 :548-554）', () => {
  it("origin='upstream' 且 mirror_pk=5（正整数）→ 上家路 claimUpstreamMirror(5)", () => {
    expect(claimTargetOf({ origin: 'upstream', order_no: '', mirror_pk: 5 })).toEqual({
      kind: 'upstream',
      mirrorPk: 5,
    })
  })

  it("origin='native' 且 mirror_pk=null → 原生路 claimOrder(order_no)", () => {
    expect(
      claimTargetOf({ origin: 'native', order_no: '10000000000000000001', mirror_pk: null }),
    ).toEqual({ kind: 'native', orderNo: '10000000000000000001' })
  })

  it("origin='native' 且 mirror_pk='x'（契约漂移）→ 原生路（pk 漂移不改变原生行的路）", () => {
    expect(
      claimTargetOf({ origin: 'native', order_no: '10000000000000000001', mirror_pk: 'x' }),
    ).toEqual({ kind: 'native', orderNo: '10000000000000000001' })
  })

  it('T2 评审 Minor-2：origin=upstream 且 mirror_pk 非正整数 ⇒ fail-closed null——哪怕单号非空也不许落原生路', () => {
    expect(
      claimTargetOf({ origin: 'upstream', order_no: '10000000000000000001', mirror_pk: 'x' }),
    ).toBeNull()
    expect(claimTargetOf({ origin: 'upstream', order_no: '10000000000000000001', mirror_pk: null })).toBeNull()
    expect(claimTargetOf({ origin: 'upstream', order_no: '10000000000000000001', mirror_pk: 0 })).toBeNull()
  })

  it('upstream 行主键不可用（合规坏形状：单号本来就是空串）→ null：不画按钮不发请求', () => {
    expect(claimTargetOf({ origin: 'upstream', order_no: '', mirror_pk: null })).toBeNull()
    expect(claimTargetOf({ origin: 'upstream', order_no: '', mirror_pk: 'x' })).toBeNull()
  })

  it('原生行单号为空 → null（不画按钮不发请求）', () => {
    expect(claimTargetOf({ origin: 'native', order_no: '', mirror_pk: null })).toBeNull()
  })

  // ── D-0296：模拟单第三路 ──
  it("D-0296：origin='simulated' 且 sim_pk=7（正整数）→ 模拟路 claimSimulatedHallOrder(7)", () => {
    expect(claimTargetOf({ origin: 'simulated', order_no: '', mirror_pk: null, sim_pk: 7 })).toEqual({
      kind: 'simulated',
      simPk: 7,
    })
  })

  it('D-0296：origin=simulated 且 sim_pk 非正整数 ⇒ fail-closed null（⚠️ 不许落原生路——模拟单不该发真接单请求）', () => {
    expect(claimTargetOf({ origin: 'simulated', order_no: '', mirror_pk: null, sim_pk: null })).toBeNull()
    expect(claimTargetOf({ origin: 'simulated', order_no: '', mirror_pk: null, sim_pk: 0 })).toBeNull()
    expect(claimTargetOf({ origin: 'simulated', order_no: '', mirror_pk: null, sim_pk: 'x' })).toBeNull()
    expect(claimTargetOf({ origin: 'simulated', order_no: '', mirror_pk: null, sim_pk: 2.5 })).toBeNull()
    // ⚠️ 模拟行带仿真单号（后端 `_payload_for` 直填 display_order_no）⇒ 这条断言钉的是
    // 「即使 order_no 非空，主键不可用也不许落原生路」，与镜像行那条判据同源。
    expect(
      claimTargetOf({ origin: 'simulated', order_no: '10000000000000000009', mirror_pk: null, sim_pk: null }),
    ).toBeNull()
  })

  // ── D-0303：团团通上家（peer）第四路 ──
  it("D-0303：origin='peer' 且 mirror_pk 正整数 → 团团通路 claimPeerMirror(镜像行主键)", () => {
    expect(claimTargetOf({ origin: 'peer', order_no: '', mirror_pk: 9 })).toEqual({
      kind: 'peer',
      mirrorPk: 9,
    })
  })

  it('D-0303：peer 行主键不可用 ⇒ fail-closed null（⛔ 不许落原生路、也不许并进 upstream 路）', () => {
    for (const bad of [null, 0, -3, 2.5, 'x', undefined]) {
      expect(claimTargetOf({ origin: 'peer', order_no: '', mirror_pk: bad })).toBeNull()
    }
    // 阴性对照：即使带着一个非空的我方单号，peer 也只走自己那一支（⛔ 不拿别人的单号去打原生端点）。
    expect(
      claimTargetOf({ origin: 'peer', order_no: '10000000000000000001', mirror_pk: null }),
    ).toBeNull()
    // 同一个主键值在两条镜像线上是**两种**路（端点不同）——⛔ 别合并这两支。
    expect(claimTargetOf({ origin: 'upstream', order_no: '', mirror_pk: 9 })).toEqual({
      kind: 'upstream',
      mirrorPk: 9,
    })
  })
})

describe('hallRowKey（卡片业务标识）', () => {
  it('原生 = 我方单号；镜像 = 上家单号（前缀防两源相撞）', () => {
    expect(hallRowKey({ origin: 'native', order_no: '10000000000000000001', remote_order_id: '' })).toBe(
      'native:10000000000000000001',
    )
    expect(
      hallRowKey({ origin: 'upstream', order_no: '', remote_order_id: 'JP10020030040050060' }),
    ).toBe('upstream:JP10020030040050060')
  })

  it('D-0296：模拟行 = sim_pk（它的 remote_order_id 恒空串，不分流会全部撞成同一个键）', () => {
    expect(hallRowKey({ origin: 'simulated', order_no: '10000000000000000009', remote_order_id: '', sim_pk: 7 })).toBe(
      'sim:7',
    )
    // 主键漂移也不与别的行相撞（空串兜底，页面渲染容错——⛔ 不抛异常）。
    expect(hallRowKey({ origin: 'simulated', order_no: '', remote_order_id: '' })).toBe('sim:')
  })

  it('D-0303：peer 行 = `peer:` + 镜像行主键；与同值 remote_order_id 的鸡蛋铺镜像行**不相撞**', () => {
    expect(
      hallRowKey({
        origin: 'peer',
        order_no: '',
        remote_order_id: '20000000000000000001',
        mirror_pk: 9,
      }),
    ).toBe('peer:9')
    // 两张表的单号空间互不相干：取值恰好相同时必须是两个不同的 key（`:key` 撞车会复用错 DOM）。
    expect(
      hallRowKey({
        origin: 'upstream',
        order_no: '',
        remote_order_id: '20000000000000000001',
        mirror_pk: 9,
      }),
    ).toBe('upstream:20000000000000000001')
    // 主键缺失（契约漂移）⇒ 回落上家单号，⛔ 不抛异常（页面渲染容错）。
    expect(
      hallRowKey({
        origin: 'peer',
        order_no: '',
        remote_order_id: '20000000000000000001',
        mirror_pk: null,
      }),
    ).toBe('peer:20000000000000000001')
  })
})

describe('hallOrderNoDisplay（卡片订单号那一行；⭐ D-0301 起原生行展示回退）', () => {
  const native = {
    origin: 'native' as const,
    order_no: '10000000000000000001',
    // ⚠️ 基准夹具来源号为空串（回落变体）；「来源号非空 ⇒ 展示来源号」的主变体在下面。
    source_order_no: '',
    remote_order_view: '',
    remote_order_id: '',
  }

  it('原生行：来源号为空 → 回落我方内置单号，标签「订单号」', () => {
    expect(hallOrderNoDisplay(native)).toEqual({
      label: '订单号',
      text: '10000000000000000001',
    })
  })

  it('⭐ D-0301（2026-10-24）原生行：来源号非空 → 逐字显示来源号（标签照旧「订单号」）', () => {
    expect(
      hallOrderNoDisplay({ ...native, source_order_no: 'SRC-TEST-1' }),
    ).toEqual({ label: '订单号', text: 'SRC-TEST-1' })
  })

  it('⭐ 原生行：来源号是空白串 → 不算「有来源号」，仍回落内置号', () => {
    expect(
      hallOrderNoDisplay({ ...native, source_order_no: '   ' }),
    ).toEqual({ label: '订单号', text: '10000000000000000001' })
  })

  it('镜像行（order_no 是空串）→ 显示远程展示单号，标签统一「订单号」（⛔ 不显示空白）', () => {
    expect(
      hallOrderNoDisplay({
        origin: 'upstream',
        order_no: '',
        source_order_no: '',
        remote_order_view: '测试远端展示单号甲',
        remote_order_id: 'JP10020030040050060',
      }),
    ).toEqual({ label: '订单号', text: '测试远端展示单号甲' })
  })

  it('镜像行：remote_order_view 为空 ⇒ 回落 remote_order_id（两源口径 = 桌面 :496-498）', () => {
    expect(
      hallOrderNoDisplay({
        origin: 'upstream',
        order_no: '',
        source_order_no: '',
        remote_order_view: '',
        remote_order_id: 'JP10020030040050060',
      }),
    ).toEqual({ label: '订单号', text: 'JP10020030040050060' })
    // 只有空白也不算有值（E-0021 同族：空白串不算有单号）。
    expect(
      hallOrderNoDisplay({
        origin: 'remote',
        order_no: '',
        source_order_no: '',
        remote_order_view: '   ',
        remote_order_id: 'JP10020030040050060',
      }),
    ).toEqual({ label: '订单号', text: 'JP10020030040050060' })
  })

  it('镜像行两源都空（数据异常）→ text 空串：页面不画这一行（⛔ 不留空壳）', () => {
    expect(
      hallOrderNoDisplay({
        origin: 'upstream',
        order_no: '',
        source_order_no: '',
        remote_order_view: '',
        remote_order_id: '',
      }),
    ).toEqual({ label: '订单号', text: '' })
  })

  it('原生行空单号（异常形状）→ text 空串，不回落远端键（⛔ 不把上家号冒充我方单）', () => {
    expect(
      hallOrderNoDisplay({
        origin: 'native',
        order_no: '',
        source_order_no: '',
        remote_order_view: '测试远端展示单号甲',
        remote_order_id: 'JP10020030040050060',
      }),
    ).toEqual({ label: '订单号', text: '' })
  })

  it('D-0303：peer 行 → 订单号（remote_order_view 优先、空则回落 remote_order_id）', () => {
    expect(
      hallOrderNoDisplay({
        origin: 'peer',
        order_no: '',
        source_order_no: '',
        remote_order_view: '20000000000000000001',
        remote_order_id: '20000000000000000001',
      }),
    ).toEqual({ label: '订单号', text: '20000000000000000001' })
    // 读口两个键都是上家单号（`peer_display_no` + `peer_order_no`）⇒ 回落取到的是同一个值。
    expect(
      hallOrderNoDisplay({
        origin: 'peer',
        order_no: '',
        source_order_no: '',
        remote_order_view: '',
        remote_order_id: '20000000000000000001',
      }),
    ).toEqual({ label: '订单号', text: '20000000000000000001' })
  })

  it('D-0296：模拟行 → 仿真单号照标「订单号」（⛔ 不标「模拟」——演示卡按普通单看，也不显示空白）', () => {
    // 后端 `_payload_for`：模拟行的 `order_no` 直填 `display_order_no`（20 位仿真号）。
    expect(
      hallOrderNoDisplay({
        origin: 'simulated',
        order_no: '10000000000000000009',
        source_order_no: '',
        remote_order_view: '',
        remote_order_id: '',
      }),
    ).toEqual({ label: '订单号', text: '10000000000000000009' })
    // 单号两侧空白也要去掉（与原生行同一条 trim 口径）。
    expect(
      hallOrderNoDisplay({
        origin: 'simulated',
        order_no: ' 10000000000000000009 ',
        source_order_no: '',
        remote_order_view: '',
        remote_order_id: '',
      }),
    ).toEqual({ label: '订单号', text: '10000000000000000009' })
  })
})
