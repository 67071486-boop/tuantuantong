// @vitest-environment jsdom
import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { READ_COLS, TABLE_SPECS, UNREAD_COLS } from '../user-mymessage'

/**
 * 2 个 `/User/*` 人员页的**表头与关键取值防漂移测试**。
 *
 * 断言的对象是**对标产物**（不是我们的实现），逐页指到源文件行号：
 *  - `.tools/recon/suqing1/assets/JS2019/User/mymessage.js` L493-551 / L587-643
 *  - `.tools/recon/suqing1/html/User__myinfo.html` L52-175（页内 `<style>`）
 *
 * 2026-09-21 业务裁剪（D-0036）删掉「邀请大神注册管理」（`user-applyonboardingmange`）、
 * 「打卡记录」（`user-checkinlog`）与「打卡分析」（`user-checkinstatic`）三页及其三个 describe，
 * 本文件从 5 页收窄到 2 页。
 *
 * 为什么值得写：这些坑 `parity.mjs` **看不见** ——
 *  1. 特殊列（`type:'checkbox'`）漏写会**静默丢列**（表头只比文案+宽度，空文案的特殊列丢了不报）；
 *  2. `defaultToolbar` 在同一 render 对象里写两次，**后写生效**（抄错成 `[]` 会少「筛选列」图标）；
 *  3. `pageLimits` 取错档位（如 `limits:[20]` 抄成默认 5 项）只在**有数据**时才显形；
 *  4. 勾选列丢失还会连带让「固定列表头组」与分页控件在探针里消失（我们恒空，探针本来就不报）。
 */
const REPLICA_DIR = path.resolve(__dirname, '../../../../public/replica')
const readArtifact = (key: string): string => fs.readFileSync(path.join(REPLICA_DIR, `${key}.html`), 'utf8')

type Extra = { type?: string; toolbar?: string; templet?: string }

describe('user-mymessage（/User/mymessage）', () => {
  it('未读表 7 列，文案序列与 mymessage.js L493-551 一致', () => {
    expect(UNREAD_COLS.length).toBe(7)
    expect(UNREAD_COLS.map((c) => c.title)).toEqual([
      undefined, // `{ type:'checkbox', fixed:'left', width:30 }`——对标就是无 title 的特殊列
      '消息标题',
      '消息分类',
      '消息内容',
      '发送人',
      '是否已读',
      '接收时间',
    ])
  })

  it('首列是 type:checkbox + fixed:left + width:30（漏 type 会静默丢勾选框）', () => {
    const first = UNREAD_COLS[0] as Extra
    expect(first.type).toBe('checkbox')
    expect(UNREAD_COLS[0]?.fixed).toBe('left')
    expect(UNREAD_COLS[0]?.width).toBe(30)
  })

  it('第 2、3 列的 field **都叫 title**（对标 L497/L507 的原文，别「修」成 msgType）', () => {
    expect(UNREAD_COLS[1]?.field).toBe('title')
    expect(UNREAD_COLS[2]?.field).toBe('title')
    expect(UNREAD_COLS[1]?.width).toBe(250)
    expect(UNREAD_COLS[2]?.width).toBe(80)
  })

  it('已读表 6 列且**没有勾选列**（对标 L587-643）', () => {
    expect(READ_COLS.length).toBe(6)
    expect(READ_COLS.some((c) => (c as unknown as Extra).type !== undefined)).toBe(false)
    expect(READ_COLS.map((c) => c.title)).toEqual([
      '消息标题',
      '消息分类',
      '消息内容',
      '发送人',
      '是否已读',
      '接收时间',
    ])
  })

  it('两张表：defaultToolbar 取生效的 [\'filter\']、cellMinWidth 150、height full、limits 5 档', () => {
    expect(TABLE_SPECS.map((t) => t.elem)).toEqual(['#unReadTableList', '#readTableList'])
    expect(TABLE_SPECS.map((t) => t.toolbar)).toEqual(['#unReadtoolbarDemo', '#readToolbarDemo'])
    for (const t of TABLE_SPECS) {
      expect(t.defaultToolbar).toEqual(['filter'])
      expect(t.cellMinWidth).toBe(150)
      expect(t.height).toBe('full')
      expect(t.limit).toBe(20)
      expect(t.pageLimits).toEqual([10, 20, 30, 40, 50])
    }
  })

  it('证据件含两个工具栏模板的**逐字**按钮（3 颗未读 / 1 颗已读），且没有未解析的可见 {{', () => {
    const html = readArtifact('user-mymessage')
    expect(html).toContain('id="unReadtoolbarDemo"')
    expect(html).toContain('id="readToolbarDemo"')
    expect(html).toContain('刷新数据')
    expect(html).toContain('标记为已读')
    expect(html).toContain('全部标记为已读')
    // 侧边页签与三层页签的中文取自该页 i18n 字典（mymessage.js L211-234）
    expect(html).toContain('急需处理')
    expect(html).toContain('重要消息')
    expect(html).toContain('一般消息')
    expect(html).toContain('系统新消息提醒')
    expect(html).toContain('一键重置消息设置')
  })
})

describe('user-myinfo（/User/myinfo）', () => {
  it('页内两条**复刻关键值**逐字在证据件里（L66-72）', () => {
    const html = readArtifact('user-myinfo')
    expect(html).toContain('.admin-info .layui-form-item .layui-form-label {')
    expect(html).toContain('width: 150px')
    expect(html).toContain('.admin-info .layui-form-item .layui-input-block {')
    expect(html).toContain('margin-left: 150px;')
  })

  it('字段 name 照抄对标的拼写 `moblie`（L301，不是 mobile）', () => {
    const html = readArtifact('user-myinfo')
    expect(html).toContain('name="moblie"')
    expect(html).not.toContain('name="mobile"')
  })

  it('rnameCertDiv 是**页签容器的兄弟节点**（不在任何 .layui-tab-item 里）', () => {
    const html = readArtifact('user-myinfo')
    const certIdx = html.indexOf('class="layui-form-pane rnameCertDiv"')
    const pwdTabIdx = html.indexOf('name="renewPwd"')
    // 修改密码页签（第 3 个 .layui-tab-item）在 rnameCertDiv **之前**结束 ⇒ 两者是兄弟。
    expect(certIdx).toBeGreaterThan(pwdTabIdx)
    expect(html.slice(pwdTabIdx, certIdx)).not.toContain('<div class="layui-tab-item"')
  })

  it('「功能设置」页签只留服务端替换后的红字（本租户不渲染绑定字段组）——⛔ 证据件里**必须有**它', () => {
    // ⭐ 这一条是**配对判据的另一半**（2026-10-08，spec §10 / §13.5 的 F1）：
    //    本页在**运行期**把那段只读红字删掉了（`user-myinfo.ts::removeReadonlyBindingNote`，
    //    业务方裁定），但**证据件一字未动**——它就钉在下面这几行上。
    //    ⇒ 「运行期没有」的判据在 `user-myinfo.spec.ts` 的 ㉓ / ㊴；两条一起才说明
    //    「该消失的消失了、该留的留住了」（⛔ 单独一条都能被「顺手改证据件」蒙过去）。
    const html = readArtifact('user-myinfo')
    expect(html).toContain('请回到您自己的管理系统里操作绑定。功能入口：系统维护-&gt;绑定上家系统设置')
    expect(html).toContain('color:#E03636')
    // 原模板那份绑定表单仍在标记里，但带 layui-hide（保持隐藏）。
    expect(html).toContain('admin-info layui-hide')
  })

  it('3 个页签中文取自 myinfo.js L291-293', () => {
    const html = readArtifact('user-myinfo')
    for (const t of ['基本资料', '功能设置', '修改密码']) expect(html).toContain(t)
  })
})
